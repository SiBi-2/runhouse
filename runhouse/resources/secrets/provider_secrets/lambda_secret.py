import copy
import os
from pathlib import Path

from typing import Optional

from runhouse.resources.blobs.file import File
from runhouse.resources.secrets.functions import _check_file_for_mismatches

from runhouse.resources.secrets.provider_secrets.provider_secret import ProviderSecret


class LambdaSecret(ProviderSecret):
    # values format: {"api_key": api_key}
    _DEFAULT_CREDENTIALS_PATH = "~/.lambda_cloud/lambda_keys"
    _PROVIDER = "lambda"

    @staticmethod
    def from_config(config: dict, dryrun: bool = False):
        return LambdaSecret(**config, dryrun=dryrun)

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
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w+") as f:
            f.write(f'api_key = {values["api_key"]}\n')

        return new_secret

    def _from_path(self, path: Optional[str] = None):
        path = path or self.path
        if isinstance(path, File):
            lines = path.fetch(mode="r").split("\n")
        if path and os.path.exists(os.path.expanduser(path)):
            with open(os.path.expanduser(path), "r") as f:
                lines = f.readlines()
        for line in lines:
            split = line.split()
            if split[0] == "api_key":
                api_key = split[-1]
                return {"api_key": api_key}
        return {}
