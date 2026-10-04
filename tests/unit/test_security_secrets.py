import pytest

from tokendrain.secrets import parse_dotenv
from tokendrain.security import SessionTokens


def test_session_auth_and_tamper() -> None:
    tokens = SessionTokens("admin-" + "x" * 32)
    cookie = tokens.issue()
    assert tokens.valid(cookie)
    assert not tokens.valid(cookie + "tampered")
    assert not tokens.valid("not-a-cookie")
    assert not tokens.valid("-1.nonce.YQ==")
    assert tokens.authenticate("admin-" + "x" * 32)
    assert not tokens.authenticate("wrong")
    assert not SessionTokens("different-" + "y" * 32).valid(cookie)


@pytest.mark.parametrize("text", ["KEY=one\nKEY=two", "KEY", "KEY='unclosed", "PATH=bad"])
def test_dotenv_rejects_ambiguous_or_reserved_values(text: str) -> None:
    with pytest.raises(ValueError):
        parse_dotenv(text, {"KEY": "purpose", "PATH": "purpose"})
