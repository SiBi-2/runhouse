import argparse
import json
import logging
import subprocess
import traceback
from pathlib import Path
from typing import Optional

import ray
import requests
from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse
from sky.skylet.autostop_lib import set_last_active_time_to_now

from runhouse.rh_config import configs, env_servlets, rns_client
from runhouse.rns.servlet import EnvServlet
from runhouse.rns.utils.names import _generate_default_name
from ..http.http_utils import (
    Args,
    b64_unpickle,
    DEFAULT_SERVER_PORT,
    Message,
    OutputType,
    pickle_b64,
    Response,
)

logger = logging.getLogger(__name__)

app = FastAPI()


class HTTPServer:
    DEFAULT_PORT = 50052
    MAX_MESSAGE_LENGTH = 1 * 1024 * 1024 * 1024  # 1 GB
    LOGGING_WAIT_TIME = 1.0
    SKY_YAML = str(Path("~/.sky/sky_ray.yml").expanduser())

    def __init__(self, conda_env=None, *args, **kwargs):
        if not ray.is_initialized():
            ray.init(
                ignore_reinit_error=True,
                runtime_env={"conda": conda_env} if conda_env else {},
                namespace="runhouse",
            )

        try:
            # Collect metadata for the cluster immediately on init
            self._collect_cluster_stats()
        except Exception as e:
            logger.error(f"Failed to collect cluster stats: {str(e)}")

        base_env = self.get_env_servlet(
            env_name="base",
            create=True,
            runtime_env={"conda": conda_env} if conda_env else {},
        )
        env_servlets["base"] = base_env
        from runhouse.rh_config import obj_store

        obj_store.set_name("server")

        HTTPServer.register_activity()

    @staticmethod
    def register_activity():
        set_last_active_time_to_now()

    @staticmethod
    @app.post("/check")
    def check_server(message: Message):
        HTTPServer.register_activity()
        cluster_config = message.data
        try:
            if cluster_config:
                logger.info(
                    f"Message received from client to check server: {cluster_config}"
                )
                rh_dir = Path("~/.rh").expanduser()
                rh_dir.mkdir(exist_ok=True)
                (rh_dir / "cluster_config.yaml").write_text(cluster_config)
                # json.dump(cluster_config, open(rh_dir / "cluster_config.yaml", "w"), indent=4)

            # Check if Ray is deadlocked
            # Get `ray status` from command line
            status = subprocess.check_output(["ray", "status"]).decode("utf-8")
            return Response(data=pickle_b64(status), output_type=OutputType.RESULT)
        except Exception as e:
            logger.exception(e)
            HTTPServer.register_activity()
            return Response(
                error=pickle_b64(e),
                traceback=pickle_b64(traceback.format_exc()),
                output_type=OutputType.EXCEPTION,
            )

    @staticmethod
    def get_env_servlet(env_name, create=False, runtime_env=None):
        if env_name in env_servlets.keys():
            return env_servlets[env_name]

        if create:
            new_env = (
                ray.remote(EnvServlet)
                .options(
                    name=env_name,
                    get_if_exists=True,
                    runtime_env=runtime_env,
                    lifetime="detached",
                    namespace="runhouse",
                    max_concurrency=1000,
                )
                .remote(env_name=env_name)
            )
            env_servlets[env_name] = new_env
            return new_env

        else:
            raise Exception(
                f"Environment {env_name} does not exist. Please send it to the cluster first."
            )

    @staticmethod
    def call_servlet_method(servlet, method, args, block=True):
        if isinstance(servlet, ray.actor.ActorHandle):
            obj_ref = getattr(servlet, method).remote(*args)
            if block:
                return ray.get(obj_ref)
            else:
                return obj_ref
        else:
            return getattr(servlet, method)(*args)

    @staticmethod
    def call_in_env_servlet(
        method,
        args=None,
        env=None,
        create=False,
        lookup_env_for_name=None,
        block=True,
    ):
        HTTPServer.register_activity()
        try:
            if lookup_env_for_name:
                env = env or HTTPServer.lookup_env_for_name(lookup_env_for_name)
            servlet = HTTPServer.get_env_servlet(env or "base", create=create)
            # If servlet is a RayActor, call with .remote
            return HTTPServer.call_servlet_method(servlet, method, args, block=block)
        except Exception as e:
            logger.exception(e)
            HTTPServer.register_activity()
            return Response(
                error=pickle_b64(e),
                traceback=pickle_b64(traceback.format_exc()),
                output_type=OutputType.EXCEPTION,
            )

    @staticmethod
    def lookup_env_for_name(name, check_rns=False):
        from runhouse.rh_config import obj_store

        env = obj_store.get_env(name)
        if env:
            return env

        # Load the resource config from rns and see if it has an "env" field
        if check_rns:
            resource_config = rns_client.load_config(name)
            if resource_config and "env" in resource_config:
                return resource_config["env"]

        return None

    @staticmethod
    @app.post("/env")
    def install(message: Message):
        return HTTPServer.call_in_env_servlet(
            "install",
            [message],
            env=message.env,
            create=True,
            lookup_env_for_name=message.key,
        )

    @staticmethod
    @app.post("/resource")
    def put_resource(message: Message):
        return HTTPServer.call_in_env_servlet(
            "put_resource",
            [message],
            env=message.env,
            create=True,
            lookup_env_for_name=message.key,
        )

    @staticmethod
    @app.post("/{module}/{method}")
    def call_module_method(module, method=None, message: Message = None):
        # Stream the logs and result (e.g. if it's a generator)
        HTTPServer.register_activity()
        try:
            method = None if method == "None" else method
            # If this is a "get" request to just return the module, do not stream logs or save by default
            message = message or Message(stream_logs=False, save=False)
            message.key = message.key or _generate_default_name(
                prefix=module
                if method == "__call__" or not method
                else f"{module}_{method}",
                precision="ms",  # Higher precision because we see collisions within the same second
            )
            env = message.env or HTTPServer.lookup_env_for_name(module)
            obj_ref = HTTPServer.call_in_env_servlet(
                "call_module_method",
                [module, method, message],
                env=env,
                create=True,
                block=False,
            )

            # Hold onto obj_refs just so we can support cancelling
            from runhouse.rh_config import obj_store

            obj_store.put(message.key + "_ref", obj_ref)

            if message.remote:
                return Response(
                    data=pickle_b64(message.key),
                    output_type=OutputType.RESULT,
                )
            return StreamingResponse(
                HTTPServer._get_results_and_logs_generator(
                    message.key, env=env, stream_logs=message.stream_logs
                ),
                media_type="application/json",
            )
        except Exception as e:
            logger.exception(e)
            HTTPServer.register_activity()
            return Response(
                error=pickle_b64(e),
                traceback=pickle_b64(traceback.format_exc()),
                output_type=OutputType.EXCEPTION,
            )

    @staticmethod
    def _get_logfiles(log_key, log_type=None):
        key_logs_path = Path(EnvServlet.RH_LOGFILE_PATH) / log_key
        if key_logs_path.exists():
            # Logs are like: `.rh/logs/key/key.[out|err]`
            glob_pattern = (
                "*.out"
                if log_type == "stdout"
                else "*.err"
                if log_type == "stderr"
                else "*.[oe][ur][tr]"
            )
            return [str(f.absolute()) for f in key_logs_path.glob(glob_pattern)]
        else:
            return None

    @staticmethod
    def open_new_logfiles(key, open_files):
        logfiles = HTTPServer._get_logfiles(key)
        if logfiles:
            for f in logfiles:
                if f not in [o.name for o in open_files]:
                    logger.info(f"Streaming logs from {f}")
                    open_files.append(open(f, "r"))
        return open_files

    @staticmethod
    def _get_results_and_logs_generator(key, env, stream_logs):
        open_logfiles = []

        waiting_for_results = True
        try:
            obj_ref = None
            while waiting_for_results:
                if not obj_ref:
                    obj_ref = HTTPServer.call_in_env_servlet(
                        "_get_result_from_stream",
                        [key],
                        env=env,
                        block=False,
                    )
                try:
                    ret_val = ray.get(obj_ref, timeout=HTTPServer.LOGGING_WAIT_TIME)
                    # Last result in a stream will have type RESULT to indicate the end
                    if ret_val is None:
                        # Still waiting for results in queue
                        obj_ref = None
                        # time.sleep(HTTPServer.LOGGING_WAIT_TIME)
                        raise ray.exceptions.GetTimeoutError
                    if not ret_val.output_type == OutputType.RESULT_STREAM:
                        waiting_for_results = False
                    ret_resp = json.dumps(jsonable_encoder(ret_val))
                    logger.info(f"Yielding response for key {key}")
                    yield ret_resp
                    obj_ref = None
                except ray.exceptions.GetTimeoutError:
                    pass
                # Grab all the lines written to all the log files since the last time we checked, including
                # any new log files that have been created
                open_logfiles = (
                    HTTPServer.open_new_logfiles(key, open_logfiles)
                    if stream_logs
                    else []
                )
                ret_lines = []
                for i, f in enumerate(open_logfiles):
                    file_lines = f.readlines()
                    if file_lines:
                        # TODO [DG] handle .out vs .err, and multiple workers
                        # if len(logfiles) > 1:
                        #     ret_lines.append(f"Process {i}:")
                        ret_lines += file_lines
                if ret_lines:
                    lines_resp = Response(
                        data=ret_lines,
                        output_type=OutputType.STDOUT,
                    )
                    logger.info(f"Yielding logs for key {key}")
                    yield json.dumps(jsonable_encoder(lines_resp))
        except Exception as e:
            logger.exception(e)
            yield json.dumps(
                jsonable_encoder(
                    Response(
                        error=pickle_b64(e),
                        traceback=pickle_b64(traceback.format_exc()),
                        output_type=OutputType.EXCEPTION,
                    )
                )
            )
        finally:
            if stream_logs and not open_logfiles:
                logger.warning(f"No logfiles found for call {key}")
            for f in open_logfiles:
                f.close()

    @staticmethod
    @app.post("/run")
    def run_module(message: Message):
        return HTTPServer.call_in_env_servlet(
            "run_module",
            [message],
            env=message.env,
            create=True,
            lookup_env_for_name=message.key,
        )

    @staticmethod
    @app.get("/object")
    def get_object(message: Message):
        HTTPServer.register_activity()
        try:
            return StreamingResponse(
                HTTPServer._get_object_and_logs_generator(message),
                media_type="application/json",
            )
        except Exception as e:
            logger.exception(e)
            HTTPServer.register_activity()
            return Response(
                error=pickle_b64(e),
                traceback=pickle_b64(traceback.format_exc()),
                output_type=OutputType.EXCEPTION,
            )

    @staticmethod
    def _get_object_and_logs_generator(message):
        key, stream_logs = b64_unpickle(message.data)
        logger.info(f"Message received from client to get object: {key}")
        # servlet = HTTPServer.get_env_servlet(message.env or "base", create=False, lookup_env_for_name=key)
        logfiles = None
        open_files = None
        ret_obj = None
        returned = False
        while not returned:
            ret_obj = HTTPServer.call_in_env_servlet(
                method="get",
                args=[key, HTTPServer.LOGGING_WAIT_TIME],
                env=message.env,
                create=False,
                lookup_env_for_name=key,
            )
            returned = ret_obj is not None
            # Don't return yet, go through the loop once more to get any remaining log lines

            if stream_logs:
                if not logfiles:
                    logfiles = HTTPServer.call_in_env_servlet(
                        method="get_logfiles",
                        args=[key],
                        env=message.env,
                        create=False,
                        lookup_env_for_name=key,
                    )
                    open_files = [open(i, "r") for i in (logfiles or [])]
                    logger.info(f"Streaming logs for {key} from {logfiles}")

                # Grab all the lines written to all the log files since the last time we checked
                ret_lines = []
                for i, f in enumerate(open_files):
                    file_lines = f.readlines()
                    if file_lines:
                        # TODO [DG] handle .out vs .err, and multiple workers
                        # if len(logfiles) > 1:
                        #     ret_lines.append(f"Process {i}:")
                        ret_lines += file_lines
                if ret_lines:
                    yield json.dumps(
                        jsonable_encoder(
                            Response(
                                data=ret_lines,
                                output_type=OutputType.STDOUT,
                            )
                        )
                    )

        if stream_logs and open_files:
            # We got the object back from the object store, so we're done (but we went through the loop once
            # more to get any remaining log lines)
            [f.close() for f in open_files]
        yield json.dumps(jsonable_encoder(ret_obj))

    @staticmethod
    @app.post("/object")
    def put_object(message: Message):
        return HTTPServer.call_in_env_servlet(
            "put_object", [message.key, message.data], env=message.env, create=True
        )

    @staticmethod
    @app.put("/object")
    def rename_object(message: Message):
        return HTTPServer.call_in_env_servlet(
            "rename_object", [message], env=message.env
        )

    @staticmethod
    @app.delete("/object")
    def delete_obj(message: Message):
        return HTTPServer.call_in_env_servlet(
            "delete_obj", [message], env=message.env, lookup_env_for_name=message.key
        )

    @staticmethod
    @app.get("/run_object")
    def get_run_object(message: Message):
        return HTTPServer.call_in_env_servlet(
            "get_run_object",
            [message],
            env=message.env,
            lookup_env_for_name=message.key,
        )

    @staticmethod
    @app.post("/cancel")
    def cancel_run(message: Message):
        return HTTPServer.call_in_env_servlet(
            "cancel_run", [message], env=message.env, lookup_env_for_name=message.key
        )

    @staticmethod
    @app.get("/keys")
    def get_keys(env: Optional[str] = None):
        from runhouse.rh_config import obj_store

        if not env:
            return Response(
                output_type=OutputType.RESULT, data=pickle_b64(obj_store.keys())
            )
        return HTTPServer.call_in_env_servlet("get_keys", [], env=env)

    @staticmethod
    @app.post("/secrets")
    def add_secrets(message: Message):
        return HTTPServer.call_in_env_servlet(
            "add_secrets", [message], env=message.env, create=True
        )

    @staticmethod
    @app.post("/call/{fn_name}")
    def call_fn(fn_name: str, args: Args):
        return HTTPServer.call_in_env_servlet(
            "call_fn", [fn_name, args], create=True, lookup_env_for_name=fn_name
        )

    @staticmethod
    def _collect_cluster_stats():
        """Collect cluster metadata and send to Grafana Loki"""
        if configs.get("disable_data_collection") is True:
            return

        cluster_data = HTTPServer._cluster_status_report()
        sky_data = HTTPServer._cluster_sky_report()

        HTTPServer._log_cluster_data(
            {**cluster_data, **sky_data},
            labels={"username": configs.get("username"), "environment": "prod"},
        )

    @staticmethod
    def _cluster_status_report():
        import ray._private.usage.usage_lib as ray_usage_lib
        from ray._private import gcs_utils

        gcs_client = gcs_utils.GcsClient(
            address="127.0.0.1:6379", nums_reconnect_retry=20
        )

        # fields : ['ray_version', 'python_version']
        cluster_metadata = ray_usage_lib.get_cluster_metadata(gcs_client)

        # fields: ['total_num_cpus', 'total_num_gpus', 'total_memory_gb', 'total_object_store_memory_gb']
        cluster_status_report = ray_usage_lib.get_cluster_status_to_report(
            gcs_client
        ).__dict__

        return {**cluster_metadata, **cluster_status_report}

    @staticmethod
    def _cluster_sky_report():
        try:
            from runhouse import Secrets

            sky_ray_data = Secrets.read_yaml_file(HTTPServer.SKY_YAML)
        except FileNotFoundError:
            # For on prem clusters we won't have sky data
            return {}

        provider = sky_ray_data["provider"]
        node_config = sky_ray_data["available_node_types"].get("ray.head.default", {})

        return {
            "cluster_name": sky_ray_data.get("cluster_name"),
            "region": provider.get("region"),
            "provider": provider.get("module"),
            "instance_type": node_config.get("node_config", {}).get("InstanceType"),
        }

    @staticmethod
    def _log_cluster_data(data: dict, labels: dict):
        from runhouse.rns.utils.api import log_timestamp

        payload = {
            "streams": [
                {"stream": labels, "values": [[str(log_timestamp()), json.dumps(data)]]}
            ]
        }

        payload = json.dumps(payload)
        resp = requests.post(
            f"{configs.get('api_server_url')}/admin/logs", data=json.dumps(payload)
        )

        if resp.status_code == 405:
            # api server not configured to receive grafana logs
            return

        if resp.status_code != 200:
            logger.error(
                f"({resp.status_code}) Failed to send logs to Grafana Loki: {resp.text}"
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--port", type=int, default=DEFAULT_SERVER_PORT, help="Port to run server on"
    )
    parser.add_argument(
        "--conda_env", type=str, default=None, help="Conda env to run server in"
    )
    parse_args = parser.parse_args()
    port = parse_args.port
    conda_name = parse_args.conda_env
    import uvicorn

    HTTPServer(conda_env=conda_name)
    uvicorn.run(app, host="127.0.0.1", port=port)
