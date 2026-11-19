"""Separate rotation of connection credentials from other workbench secrets."""

from cryptography.fernet import Fernet, InvalidToken, MultiFernet
from sqlalchemy import text


class ConnectionKeys(MultiFernet):
    def __init__(self, keys):
        ciphers = [Fernet(key) for key in keys]
        super().__init__(ciphers)
        self.primary = ciphers[0]

    def rewrap(self, ciphertext):
        token = ciphertext.encode()
        try:
            self.primary.decrypt(token)
            return None
        except InvalidToken:
            fresh = self.rotate(token)
            if self.primary.decrypt(fresh) != self.decrypt(token):
                raise ValueError(
                    "Connection key rotation verification failed"
                ) from None
            return fresh.decode()


def connection_keys(settings):
    keys = [value.get_secret_value() for value in settings.workbench_connection_keys]
    if not keys and settings.workbench_credential_key:
        keys = [settings.workbench_credential_key]
    return ConnectionKeys(keys) if keys else None


async def rotate_connection_keys(session, keys):
    # One atomic maintenance transaction. Each batch locks in stable key order.
    changed = 0
    for table, identity, column, condition in (
        (
            "workbench_capability_assets",
            "id",
            "secret",
            "kind='connection' AND secret IS NOT NULL",
        ),
        ("workbench_connection_oauth", "state_hash", "verifier", "expires_at>now()"),
    ):
        cursor = None
        while True:
            continuation = f" AND {identity}>:cursor" if cursor is not None else ""
            batch = await session.execute(
                text(
                    f"SELECT {identity},{column} FROM {table} WHERE {condition}{continuation} ORDER BY {identity} LIMIT 100 FOR UPDATE"
                ),
                {"cursor": cursor} if continuation else {},
            )
            rows = batch.all()
            for row_id, existing in rows:
                replacement = keys.rewrap(existing)
                if replacement is not None:
                    await session.execute(
                        text(
                            f"UPDATE {table} SET {column}=:value WHERE {identity}=:key"
                        ),
                        {"value": replacement, "key": row_id},
                    )
                    changed += 1
            if len(rows) < 100:
                break
            cursor = rows[-1][0]
    return changed
