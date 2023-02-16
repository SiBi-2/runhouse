import os
import subprocess
from pathlib import Path
from typing import Optional

from runhouse import Secrets


class HuggingFaceSecrets(Secrets):
    PROVIDER_NAME = "huggingface"
    CREDENTIALS_FILE = os.path.expanduser("~/.huggingface/token")

    @classmethod
    def read_secrets(cls, from_env: bool = False, file_path: Optional[str] = None):
        if from_env:
            raise NotImplementedError(
                f"Reading secrets from env is not supported for {cls.PROVIDER_NAME}"
            )
        else:
            creds_path = file_path or cls.default_credentials_path()
            token = Path(creds_path).read_text()

        return {"provider": cls.PROVIDER_NAME, "token": token}

    @classmethod
    def save_secrets(
        cls, secrets: dict, file_path: Optional[str] = None, overwrite: bool = False
    ):
        # TODO check properly if hf needs to be installed
        try:
            import huggingface_hub
        except ModuleNotFoundError:
            subprocess.run(["pip", "install", "--upgrade", "huggingface-hub"])
            import huggingface_hub

        dest_path = file_path or cls.default_credentials_path()
        if cls.has_secrets_file() and not overwrite:
            cls.check_secrets_for_mismatches(
                secrets_to_save=secrets, file_path=dest_path
            )
            return

        huggingface_hub.login(token=secrets["token"])
        cls.save_secret_to_config()
