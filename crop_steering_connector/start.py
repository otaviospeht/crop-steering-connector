"""Container entry: `/app` on the path explicitly, because `python3 -I` adds neither the script folder nor the cwd."""

import sys

sys.path.insert(0, "/app")

from connector.main import main  # noqa: E402

raise SystemExit(main())
