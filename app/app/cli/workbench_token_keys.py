"""D10：生成工作台令牌签名钥对（ES256）。运维一次性跑，私钥进 Secret，公钥给边缘与知识服务。

    python -m app.cli.workbench_token_keys --private-out /tmp/wb-signing.pem --public-out /tmp/wb-public.pem

只写文件（0600），不往 stdout 打私钥。已有私钥想导出公钥：--from-private /path/to/private.pem --public-out …
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from app.application.workbench.tokens import TokenIssuer, generate_private_key_pem


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--private-out", help="where to write the new private key PEM (mode 0600)"
    )
    parser.add_argument(
        "--public-out", required=True, help="where to write the public key PEM"
    )
    parser.add_argument(
        "--from-private",
        help="derive the public key from this private PEM instead of generating",
    )
    return parser.parse_args(argv)


def write_private(path: str, pem: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(pem)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    private = args.from_private or args.private_out
    if private:
        source, target = Path(private), Path(args.public_out)
        if source.resolve() == target.resolve() or (
            source.exists() and target.exists() and source.samefile(target)
        ):
            print("private and public key paths must differ", file=sys.stderr)
            return 2
    if args.from_private:
        with open(args.from_private) as f:
            private_pem = f.read()
    else:
        if not args.private_out:
            print("--private-out is required when generating", file=sys.stderr)
            return 2
        private_pem = generate_private_key_pem()
        write_private(args.private_out, private_pem)
    issuer = TokenIssuer(private_pem)
    with open(args.public_out, "w") as f:
        f.write(issuer.public_pem())
    print(
        f"kid={issuer.kid} public={args.public_out}"
        + (f" private={args.private_out}" if not args.from_private else "")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
