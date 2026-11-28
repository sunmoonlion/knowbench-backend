"""D10 钥对 CLI：私钥 0600 且不进 stdout；公钥能验该私钥签的令牌；从已有私钥导公钥一致。"""

from __future__ import annotations

import os

from app.application.workbench.tokens import TokenIssuer, verify
from app.cli.workbench_token_keys import main


def test_keygen_and_derive(tmp_path, capsys):
    priv, pub = tmp_path / "priv.pem", tmp_path / "pub.pem"
    assert main(["--private-out", str(priv), "--public-out", str(pub)]) == 0
    out = capsys.readouterr().out
    assert "PRIVATE" not in out and "kid=" in out
    assert oct(os.stat(priv).st_mode & 0o777) == "0o600"
    token = TokenIssuer(priv.read_text()).agent_token("u-1").token
    assert verify(token, pub.read_text(), audience="relay")["sub"] == "u-1"
    pub2 = tmp_path / "pub2.pem"
    assert main(["--from-private", str(priv), "--public-out", str(pub2)]) == 0
    assert pub2.read_text() == pub.read_text()
    assert main(["--public-out", str(tmp_path / "x.pem")]) == 2


def test_output_alias_never_overwrites_private_key(tmp_path):
    import os

    private = tmp_path / "key.pem"
    assert main(["--private-out", str(private), "--public-out", str(private)]) == 2
    assert not private.exists()
    assert (
        main(["--private-out", str(private), "--public-out", str(tmp_path / "pub")])
        == 0
    )
    alias = tmp_path / "linked"
    os.link(private, alias)
    before = private.read_bytes()
    assert main(["--from-private", str(private), "--public-out", str(alias)]) == 2
    assert private.read_bytes() == before
