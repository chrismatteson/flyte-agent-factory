"""`FLYTE_AUTH_COMMAND`: print a Union access token for the API key in `AGENT_FACTORY_UNION_KEY`.

The SDK runs this (`AsyncCommandAuthenticator`) whenever it needs a token, and takes stdout as the token. The
key arrives through the environment, never argv, because the SDK logs the command line. Nothing but the token
goes to stdout, and nothing secret goes to stderr.
"""

from __future__ import annotations

import asyncio
import os
import sys

from . import _errors, _union_identity


async def _token(key: str) -> str:
    # The exchange the SDK itself does for an API key (`init_from_api_key`): the token endpoint comes from the
    # server's client config. The keyring is off so a token is never written to disk.
    from flyte.remote._client.auth._auth_utils import decode_api_key
    from flyte.remote._client.auth._authenticators.client_credentials import ClientCredentialsAuthenticator
    from flyte.remote._client.auth._client_config import RemoteClientConfigStore
    from flyte.remote._client.auth._session import normalize_rpc_endpoint

    endpoint, client_id, secret, _org = decode_api_key(key)
    endpoint = normalize_rpc_endpoint(endpoint)
    authenticator = ClientCredentialsAuthenticator(
        client_id=client_id,
        client_credentials_secret=secret,
        endpoint=endpoint,
        cfg_store=RemoteClientConfigStore(endpoint),
        disable_keyring=True,
    )
    await authenticator.refresh_credentials()
    creds = authenticator.get_credentials()
    if creds is None or not creds.access_token:
        raise RuntimeError("no access token returned")
    return creds.access_token


def main() -> int:
    tier = os.environ.get(_union_identity.TIER_ENV, "unknown")
    secret = os.environ.get(_union_identity.SECRET_ENV, "unknown")
    try:
        key = os.environ.get(_union_identity.KEY_ENV, "")
        if not key:
            raise RuntimeError(f"{_union_identity.KEY_ENV} is not set")
        token = asyncio.run(_token(key))
    except _errors.REMOTE_ERRORS as exc:
        # The class name only: a message may carry the key or the server's reply.
        print(f"union token for tier {tier} (secret {secret}) failed: {type(exc).__name__}", file=sys.stderr)
        return 2
    print(token)
    return 0


if __name__ == "__main__":
    sys.exit(main())
