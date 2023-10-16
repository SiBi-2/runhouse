import copy
import json
import os
from pathlib import Path

from typing import Optional

from runhouse.resources.blobs.file import File
from runhouse.resources.secrets.functions import _check_file_for_mismatches

from runhouse.resources.secrets.provider_secrets.provider_secret import ProviderSecret


class GCPSecret(ProviderSecret):
    _DEFAULT_CREDENTIALS_PATH = "~/.config/gcloud/application_default_credentials.json"
    _PROVIDER = "gcp"
    _ENV_VARS = {
        "client_id": "CLIENT_ID",
        "client_secret": "CLIENT_SECRET",
    }

    @staticmethod
    def from_config(config: dict, dryrun: bool = False):
        return GCPSecret(**config, dryrun=dryrun)

    def write(
        self,
        path: str = None,
        overwrite: bool = False,
    ):
        new_secret = copy.deepcopy(self)
        if path:
            new_secret.path = path
        path = path or self.path
        path = os.path.expanduser(path)
        if os.path.exists(path) and _check_file_for_mismatches(
            path, self._from_path(path), self.values, overwrite
        ):
            return self

        values = self.values
        config = {}
        if Path(path).exists():
            with open(path, "r") as config_file:
                config = json.load(config_file)
        for key in values.keys():
            config[key] = values[key]

        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w+") as f:
            json.dump(config, f, indent=4)

        return new_secret

    def _from_path(self, path: Optional[str] = None):
        path = path or self.path
        config = {}
        if isinstance(path, File):
            contents = path.fetch(mode="r")
            config = json.laods(contents)
        elif path and os.path.exists(os.path.expanduser(path)):
            with open(os.path.expanduser(path), "r") as config_file:
                config = json.load(config_file)
        # if config:
        #     client_id = config["client_id"]
        #     client_secret = config["client_secret"]

        #     return {
        #         "client_id": client_id,
        #         "client_secret": client_secret,
        #     }
        return config
