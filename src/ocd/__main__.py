"""Allow invoking OCD with ``python -m ocd``."""

from .cli import main


if __name__ == "__main__":
    raise SystemExit(main())
