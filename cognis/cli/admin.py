"""CLI admin commands."""

from __future__ import annotations

from typing import Any

import typer
from argon2 import PasswordHasher
from sqlalchemy.ext.asyncio import async_sessionmaker

from cognis.bootstrap import bootstrap_runtime
from cognis.config import ENV_TEMPLATE, load_config
from cognis.security import create_password_hasher, generate_api_key_material
from cognis.store.queries import create_api_key, create_user, list_api_keys, update_user_password

admin_app = typer.Typer(help="Direct DB admin commands")
api_key_app = typer.Typer(help="API key commands")
admin_app.add_typer(api_key_app, name="api-key")


async def _get_runtime() -> tuple[object, PasswordHasher, async_sessionmaker[Any]]:
    config = load_config()
    password_hasher = create_password_hasher()
    _, _, session_factory, _ = await bootstrap_runtime(config, password_hasher)
    return config, password_hasher, session_factory


@admin_app.command("create-user")
def create_user_command(email: str, name: str | None = typer.Option(None, "--name")) -> None:
    import asyncio

    async def _run() -> None:
        _, password_hasher, session_factory = await _get_runtime()
        password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
        async with session_factory() as session:
            await create_user(
                session,
                email=email,
                name=name,
                password_hash=password_hasher.hash(password),
                role="user",
            )
            await session.commit()
        typer.echo(f"Created user {email}")

    asyncio.run(_run())


@admin_app.command("reset-password")
def reset_password_command(email: str) -> None:
    import asyncio

    async def _run() -> None:
        _, password_hasher, session_factory = await _get_runtime()
        password = typer.prompt("New password", hide_input=True, confirmation_prompt=True)
        async with session_factory() as session:
            updated = await update_user_password(session, email, password_hasher.hash(password))
            await session.commit()
        if not updated:
            raise typer.Exit(code=1)
        typer.echo(f"Updated password for {email}")

    asyncio.run(_run())


@api_key_app.command("create")
def create_api_key_command(email: str, name: str = typer.Option(..., "--name")) -> None:
    import asyncio

    async def _run() -> None:
        _, password_hasher, session_factory = await _get_runtime()
        key_id, plaintext = generate_api_key_material()
        async with session_factory() as session:
            await create_api_key(
                session,
                user_email=email,
                key_hash=password_hasher.hash(plaintext),
                name=name,
                key_id=key_id,
            )
            await session.commit()
        typer.echo(plaintext)

    asyncio.run(_run())


@api_key_app.command("list")
def list_api_keys_command(email: str) -> None:
    import asyncio

    async def _run() -> None:
        _, _, session_factory = await _get_runtime()
        async with session_factory() as session:
            items = await list_api_keys(session, email)
        for item in items:
            typer.echo(f"{item.key_id}\t{item.name}\t{item.created_at}")

    asyncio.run(_run())


def print_env_template() -> None:
    typer.echo(ENV_TEMPLATE)
