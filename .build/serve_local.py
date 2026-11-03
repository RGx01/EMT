#!/usr/bin/env python3
"""EMT's pages on this machine, against a database you choose — no Home Assistant, no Octopus.

For looking at a change in the browser: the demo household (`.build/demo_db.py`), or a COPY of a
backup's `blocks.db` (never the live file — the pages write to it). An event loop stands in for the
engine's, so the pages' writes work as they do on an install, but nothing reads a sensor or asks a
supplier: what you see is what is in the database. No `engine_startup` and no engine tick run, so a
new install's first run is not what this shows — tests/test_fresh_install.py is.

    python .build/serve_local.py <data dir> [port]     # <data dir>/blocks.db; port 8766 by default

It binds 127.0.0.1 only (the add-on binds every interface, behind ingress). Backups go to
`<data dir>/backup/`. The footer reads "Local", or EMT_INSTANCE_NAME.
"""
import asyncio
import logging
import os
import sys
import threading

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path[:0] = [_ROOT, os.path.join(_ROOT, "web")]


def main(argv):
    if len(argv) not in (2, 3):
        print(__doc__.strip().split("\n\n")[2].strip(), file=sys.stderr)
        return 2
    data = os.path.abspath(argv[1])
    db = os.path.join(data, "blocks.db")
    if not os.path.exists(db):
        print("no blocks.db in %s" % data, file=sys.stderr)
        return 1
    port = int(argv[2]) if len(argv) == 3 else 8766
    os.environ["EMT_PORT"] = str(port)
    os.environ.setdefault("EMT_INSTANCE_NAME", "Local")
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())

    import engine
    import store_access
    from block_store import BlockStore
    engine.DATA_DIR = data
    engine.SHARE_BACKUP_DIR = os.path.join(data, "backup")     # no /share off an install
    store_access.bind(db)
    engine._store = BlockStore(db)          # the engine's store, on the loop's thread (this one)

    import web.server as server
    from waitress import serve
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    server.init(data, None)
    threading.Thread(target=serve, args=(server.app,), daemon=True, name="flask",
                     kwargs={"host": "127.0.0.1", "port": port, "threads": 4}).start()
    print("EMT on http://127.0.0.1:%d — Ctrl-C to stop" % port)
    try:
        loop.run_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
