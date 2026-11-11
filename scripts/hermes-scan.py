"""Run Hermes's install scan over the Hivemind plugin, as `hermes plugins install` does.

Usage: python3 -I scripts/hermes-scan.py HERMES_SOURCE PLUGIN_DIR

Prints Hermes's scan report (whose Decision line is Hermes's policy for
skills, not plugins) and exits 1 when Hermes would refuse the
install even with --force, which is what a "dangerous" verdict does.
`make hermes-scan` fetches the pinned Hermes source and runs this.
"""

import sys
from pathlib import Path


def main(hermes_source: Path, plugin_dir: Path) -> int:
    sys.path.insert(0, str(hermes_source))
    from tools.plugin_guard import format_scan_report, scan_plugin, should_allow_plugin_install

    result = scan_plugin(plugin_dir, source="monochrome-muncher/hivemind")
    print(format_scan_report(result))
    allowed, reason = should_allow_plugin_install(result, force=True)
    if allowed:
        print(f"\nHermes installs the plugin with --force: {reason}")
        return 0
    print(f"\nHermes refuses to install the plugin, even with --force: {reason}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    if len(sys.argv) != 3:
        sys.exit(__doc__)
    sys.exit(main(Path(sys.argv[1]).resolve(), Path(sys.argv[2]).resolve()))
