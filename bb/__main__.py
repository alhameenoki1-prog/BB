"""Run the hosted web app: ``python -m bb`` (or the ``bb`` script)."""

import os


def main():
    import uvicorn

    from bb.app import create_app

    host = os.environ.get("BB_HOST", "0.0.0.0")
    port = int(os.environ.get("BB_PORT", "8000"))
    uvicorn.run(create_app(), host=host, port=port, proxy_headers=True,
                forwarded_allow_ips="*")


if __name__ == "__main__":
    main()
