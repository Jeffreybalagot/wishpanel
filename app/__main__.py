"""Entry point: `python -m app` or the `wishpanel` console script."""

from __future__ import annotations

import uvicorn

from app.config import BootstrapConfig


def main() -> None:
    cfg = BootstrapConfig.from_env()
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host="0.0.0.0",  # noqa: S104 - container service, must bind all interfaces
        port=cfg.port,
        log_level=cfg.log_level,
        proxy_headers=True,
        forwarded_allow_ips=cfg.forwarded_allow_ips,
        access_log=False,  # we emit our own structured access lines
        log_config=None,  # logging is configured by app.core.logging
    )


if __name__ == "__main__":
    main()
