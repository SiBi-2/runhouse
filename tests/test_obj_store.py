import inspect
import logging
import time
import unittest

import pytest

import runhouse as rh
from runhouse import Package

from tests.test_function import multiproc_torch_sum

TEMP_FILE = "my_file.txt"
TEMP_FOLDER = "~/runhouse-tests"

logger = logging.getLogger(__name__)


def do_printing_and_logging():
    for i in range(6):
        # Wait to make sure we're actually streaming
        time.sleep(1)
        print(f"Hello from the cluster! {i}")
        logger.info(f"Hello from the cluster logs! {i}")
    return list(range(50))


def do_tqdm_printing_and_logging(steps=6):
    from tqdm.auto import tqdm  # progress bar

    progress_bar = tqdm(range(steps))
    for i in range(steps):
        # Wait to make sure we're actually streaming
        time.sleep(0.1)
        progress_bar.update(1)
    return list(range(50))


@pytest.mark.clustertest
def test_get_from_cluster(cpu_cluster):
    print_fn = rh.function(fn=do_printing_and_logging, system=cpu_cluster)
    run_obj = print_fn.run(run_name="my_run")
    assert isinstance(run_obj, rh.Run)

    res = cpu_cluster.get(run_obj.name, stream_logs=True)
    assert res == list(range(50))


@pytest.mark.clustertest
def test_put_and_get_on_cluster(cpu_cluster):
    test_list = list(range(5, 50, 2)) + ["a string"]
    cpu_cluster.put("my_list", test_list)
    ret = cpu_cluster.get("my_list")
    assert all(a == b for (a, b) in zip(ret, test_list))


@pytest.mark.clustertest
@pytest.mark.parametrize("env", [None, "base", "pytorch"])
def test_call_module_method(cpu_cluster, env):
    cpu_cluster.put("numpy_pkg", Package.from_string("numpy"), env=env)

    # Test for method
    res = cpu_cluster.call_module_method(
        "numpy_pkg", "_detect_cuda_version_or_cpu", stream_logs=True
    )
    assert res == "cpu"

    # Test for property
    res = cpu_cluster.call_module_method(
        "numpy_pkg", "config_for_rns", stream_logs=True
    )
    numpy_config = Package.from_string("numpy").config_for_rns
    assert res
    assert isinstance(res, dict)
    assert res == numpy_config

    # Test iterator
    cpu_cluster.put("config_dict", list(numpy_config.keys()), env=env)
    res = cpu_cluster.call_module_method("config_dict", "__iter__", stream_logs=True)
    # Checks that all the keys in numpy_config were returned
    inspect.isgenerator(res)
    for key in res:
        assert key
        numpy_config.pop(key)
    assert not numpy_config


def pinning_helper(key=None):
    if not isinstance(key, str):
        return key + ["Found in args!"]

    from_obj_store = rh.blob(name=key + "_inside").fetch()
    if from_obj_store:
        return "Found in obj store!"

    rh.blob(name=key + "_inside", data=["put within fn"] * 5)
    return ["fn result"] * 3


@pytest.mark.clustertest
def test_pinning_and_arg_replacement(cpu_cluster):
    cpu_cluster.delete_keys()
    pin_fn = rh.function(pinning_helper).to(cpu_cluster)

    # First run should pin "run_pin" and "run_pin_inside"
    pin_fn.run(key="run_pin", run_name="pinning_test")
    print(cpu_cluster.list_keys())
    assert cpu_cluster.get("pinning_test") == ["fn result"] * 3
    assert cpu_cluster.get("run_pin_inside") == ["put within fn"] * 5

    # Subsequent runs should find replaced values in args
    assert pin_fn("pinning_test") == ["fn result"] * 3 + ["Found in args!"]
    assert pin_fn("run_pin_inside") == ["put within fn"] * 5 + ["Found in args!"]

    # When we just ran with the arg "run_pin", we put a new pin called "pinning_test_inside"
    # from within the fn. Running again should return it.
    assert pin_fn("run_pin") == "Found in obj store!"

    put_pin_value = ["put_pin_value"] * 4
    cpu_cluster.put("put_pin_inside", put_pin_value)
    assert pin_fn("put_pin") == "Found in obj store!"
    cpu_cluster.put("put_pin_outside", put_pin_value)
    assert pin_fn("put_pin_outside") == put_pin_value + ["Found in args!"]


@pytest.mark.clustertest
def test_put_resource(cpu_cluster, test_env):
    test_env.name = "test_env"
    cpu_cluster.put_resource(test_env)
    assert cpu_cluster.get("test_env").config_for_rns == test_env.config_for_rns

    assert cpu_cluster.call_module_method("test_env", "config_for_rns", stream_logs=True) == test_env.config_for_rns
    assert cpu_cluster.call_module_method("test_env", "name", stream_logs=True) == "test_env"


@pytest.mark.clustertest
def test_fault_tolerance(cpu_cluster):
    cpu_cluster.delete_keys()
    cpu_cluster.put("my_list", list(range(5, 50, 2)) + ["a string"])
    cpu_cluster.restart_server(restart_ray=False, resync_rh=False)
    ret = cpu_cluster.get("my_list")
    assert all(a == b for (a, b) in zip(ret, list(range(5, 50, 2)) + ["a string"]))


def serialization_helper_1():
    import torch

    tensor = torch.zeros(100).cuda()
    rh.pin_to_memory("torch_tensor", tensor)


def serialization_helper_2():
    tensor = rh.get_pinned_object("torch_tensor")
    return tensor.device()  # Should succeed if array hasn't been serialized


@unittest.skip
@pytest.mark.clustertest
@pytest.mark.gputest
def test_pinning_to_gpu(k80_gpu_cluster):
    # Based on the following quirk having to do with Numpy objects becoming immutable if they're serialized:
    # https://docs.ray.io/en/latest/ray-core/objects/serialization.html#fixing-assignment-destination-is-read-only
    k80_gpu_cluster.delete_keys()
    fn_1 = rh.function(serialization_helper_1).to(k80_gpu_cluster)
    fn_2 = rh.function(serialization_helper_2).to(k80_gpu_cluster)
    fn_1()
    fn_2()


@pytest.mark.clustertest
def test_stream_logs(cpu_cluster):
    print_fn = rh.function(fn=do_printing_and_logging, system=cpu_cluster)
    res = print_fn(stream_logs=True)
    # TODO [DG] assert that the logs are streamed
    assert res == list(range(50))


@pytest.mark.clustertest
def test_multiprocessing_streaming(cpu_cluster):
    re_fn = rh.function(
        multiproc_torch_sum, system=cpu_cluster, env=["./", "torch==1.12.1"]
    )
    summands = list(zip(range(5), range(4, 9)))
    res = re_fn(summands, stream_logs=True)
    assert res == [4, 6, 8, 10, 12]


@pytest.mark.clustertest
def test_tqdm_streaming(cpu_cluster):
    # Note, this doesn't work properly in PyCharm due to incomplete
    # support for carriage returns in the PyCharm console.
    print_fn = rh.function(fn=do_tqdm_printing_and_logging, system=cpu_cluster)
    res = print_fn(steps=40, stream_logs=True)
    assert res == list(range(50))


@pytest.mark.clustertest
def test_cancel_run(cpu_cluster):
    print_fn = rh.function(fn=do_printing_and_logging, system=cpu_cluster)
    run_obj = print_fn.run()
    assert isinstance(run_obj, rh.Run)

    key = run_obj.name
    cpu_cluster.cancel(key)
    with pytest.raises(Exception) as e:
        cpu_cluster.get(key, stream_logs=True)
    # NOTE [DG]: For some reason the exception randomly returns in different formats
    assert "ray.exceptions.TaskCancelledError" in str(
        e.value
    ) or "This task or its dependency was cancelled by" in str(e.value)


if __name__ == "__main__":
    unittest.main()
