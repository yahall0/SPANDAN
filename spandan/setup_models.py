"""Download model weights only. The running application never downloads models."""

import shutil
import subprocess

import httpx

from .models import NLI_DIR, NLI_FILES, NLI_REPO, NLI_REVISION, LocalModels


def main():
    ollama = shutil.which('ollama')
    if not ollama:
        raise SystemExit('Install Ollama and start its local service first.')
    models = LocalModels()
    try:
        installed = models.inventory()
        for model in dict.fromkeys((models.a, models.b, models.verifier)):
            if model.endswith('-cloud') or ':cloud' in model:
                raise SystemExit('Select downloadable local Ollama models.')
            if model not in installed:
                subprocess.run([ollama, 'pull', model], check=True)
    finally:
        models.close()
    NLI_DIR.mkdir(parents=True, exist_ok=True)
    with httpx.Client(follow_redirects=True, timeout=90) as client:
        for filename, remote in NLI_FILES.items():
            destination = NLI_DIR / filename
            if destination.is_file():
                continue
            print(f'Downloading {NLI_REPO}/{remote}', flush=True)
            temporary = destination.with_suffix('.part')
            with client.stream('GET', f'https://huggingface.co/{NLI_REPO}/resolve/{NLI_REVISION}/{remote}') as response:
                response.raise_for_status()
                with temporary.open('wb') as target:
                    for chunk in response.iter_bytes():
                        target.write(chunk)
            temporary.replace(destination)


if __name__ == '__main__':
    main()
