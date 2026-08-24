"""Create the isolated pyfragment runtime required by Fragment Stars."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
import venv
from pathlib import Path

PYFRAGMENT_REQUIREMENT = "pyfragment>=2026.3.4,<2027"


def runtime_python(venv_dir: Path) -> Path:
    relative = (
        Path("Scripts/python.exe") if sys.platform == "win32" else Path("bin/python")
    )
    return venv_dir / relative


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Установка pyfragment в отдельное окружение FunPayCardinal"
    )
    parser.add_argument(
        "--cardinal-dir",
        type=Path,
        default=Path.cwd(),
        help="корень FunPayCardinal (по умолчанию текущая папка)",
    )
    parser.add_argument(
        "--skip-pip-upgrade",
        action="store_true",
        help="не обновлять pip перед установкой",
    )
    args = parser.parse_args()

    cardinal_dir = args.cardinal_dir.expanduser().resolve()
    if not (cardinal_dir / "cardinal.py").is_file():
        parser.error(
            f"{cardinal_dir} не похожа на корень FunPayCardinal: нет cardinal.py"
        )

    venv_dir = cardinal_dir / "storage" / "plugins" / "fragment_stars" / "venv"
    python_executable = runtime_python(venv_dir)
    print(f"Создаю изолированное окружение: {venv_dir}")
    venv.EnvBuilder(with_pip=True).create(venv_dir)

    if not args.skip_pip_upgrade:
        subprocess.run(
            [str(python_executable), "-m", "pip", "install", "--upgrade", "pip"],
            check=True,
        )
    subprocess.run(
        [str(python_executable), "-m", "pip", "install", PYFRAGMENT_REQUIREMENT],
        check=True,
    )

    check = subprocess.run(
        [
            str(python_executable),
            "-c",
            (
                "import json; from importlib.metadata import version; "
                "print(json.dumps({'pyfragment': version('pyfragment')}))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    package_info = json.loads(check.stdout.strip())
    print(f"Готово: pyfragment {package_info['pyfragment']}")
    print(
        "runtime.python_executable можно оставить пустым — используется стандартный путь."
    )
    print(f"Полный путь: {python_executable}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
