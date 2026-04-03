"""CLI admin commands."""

from __future__ import annotations

from typing import Any

import typer
from argon2 import PasswordHasher
from sqlalchemy.ext.asyncio import async_sessionmaker

from cognis.bootstrap import bootstrap_runtime
from cognis.config import ENV_TEMPLATE, load_config
from cognis.security import create_password_hasher, generate_api_key_material
from cognis.store.queries import (
    count_admins,
    create_api_key,
    create_user,
    delete_user_cascade,
    disable_user,
    enable_user,
    get_user,
    list_api_keys,
    list_users,
    update_user,
    update_user_password,
)

admin_app = typer.Typer(help="Direct DB admin commands")
api_key_app = typer.Typer(help="API key commands")
admin_app.add_typer(api_key_app, name="api-key")

VALID_ROLES = {"admin", "user", "viewer", "service"}


async def _get_runtime() -> tuple[object, PasswordHasher, async_sessionmaker[Any]]:
    config = load_config()
    password_hasher = create_password_hasher()
    _, _, session_factory, _ = await bootstrap_runtime(config, password_hasher)
    return config, password_hasher, session_factory


@admin_app.command("create-user")
def create_user_command(
    email: str,
    name: str | None = typer.Option(None, "--name"),
    role: str = typer.Option("user", "--role", help="User role: admin, user, viewer, service"),
) -> None:
    import asyncio

    if role not in VALID_ROLES:
        typer.echo(f"Invalid role: {role}. Must be one of: {', '.join(sorted(VALID_ROLES))}")
        raise typer.Exit(code=1)

    async def _run() -> None:
        _, password_hasher, session_factory = await _get_runtime()
        password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
        async with session_factory() as session:
            existing = await get_user(session, email)
            if existing is not None:
                typer.echo(f"User {email} already exists")
                raise typer.Exit(code=1)
            await create_user(
                session,
                email=email,
                name=name,
                password_hash=password_hasher.hash(password),
                role=role,
            )
            await session.commit()
        typer.echo(f"Created user {email} (role={role})")

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
            typer.echo(f"User {email} not found")
            raise typer.Exit(code=1)
        typer.echo(f"Updated password for {email}")

    asyncio.run(_run())


@admin_app.command("list-users")
def list_users_command(
    include_disabled: bool = typer.Option(
        False, "--include-disabled", help="Include disabled users"
    ),
) -> None:
    """List all users."""
    import asyncio

    async def _run() -> None:
        _, _, session_factory = await _get_runtime()
        async with session_factory() as session:
            users = await list_users(session, include_disabled=include_disabled)
        if not users:
            typer.echo("No users found.")
            return
        # Header
        typer.echo(f"{'EMAIL':<40} {'NAME':<20} {'ROLE':<10} {'ACTIVE':<8} {'LAST LOGIN':<20}")
        typer.echo("-" * 100)
        for u in users:
            last_login = str(u.last_login_at)[:19] if u.last_login_at else "never"
            active = "yes" if u.is_active else "no"
            typer.echo(
                f"{u.email:<40} {(u.name or ''):<20} {u.role:<10} {active:<8} {last_login:<20}"
            )

    asyncio.run(_run())


@admin_app.command("set-role")
def set_role_command(email: str, role: str) -> None:
    """Change a user's role."""
    import asyncio

    if role not in VALID_ROLES:
        typer.echo(f"Invalid role: {role}. Must be one of: {', '.join(sorted(VALID_ROLES))}")
        raise typer.Exit(code=1)

    async def _run() -> None:
        _, _, session_factory = await _get_runtime()
        async with session_factory() as session:
            target = await get_user(session, email)
            if target is None:
                typer.echo(f"User {email} not found")
                raise typer.Exit(code=1)
            # Guard: cannot demote the last admin
            if target.role == "admin" and role != "admin":
                admin_count = await count_admins(session)
                if admin_count <= 1:
                    typer.echo("Cannot demote the last admin user")
                    raise typer.Exit(code=1)
            await update_user(session, email, role=role)
            await session.commit()
        typer.echo(f"Updated {email} role to {role}")

    asyncio.run(_run())


@admin_app.command("disable-user")
def disable_user_command(email: str) -> None:
    """Disable a user (soft delete)."""
    import asyncio

    async def _run() -> None:
        _, _, session_factory = await _get_runtime()
        async with session_factory() as session:
            target = await get_user(session, email)
            if target is None:
                typer.echo(f"User {email} not found")
                raise typer.Exit(code=1)
            if not target.is_active:
                typer.echo(f"User {email} is already disabled")
                raise typer.Exit(code=1)
            if target.role == "admin":
                admin_count = await count_admins(session)
                if admin_count <= 1:
                    typer.echo("Cannot disable the last admin user")
                    raise typer.Exit(code=1)
            await disable_user(session, email, disabled_by="cli-admin")
            await session.commit()
        typer.echo(f"Disabled user {email}")

    asyncio.run(_run())


@admin_app.command("enable-user")
def enable_user_command(email: str) -> None:
    """Re-enable a disabled user."""
    import asyncio

    async def _run() -> None:
        _, _, session_factory = await _get_runtime()
        async with session_factory() as session:
            target = await get_user(session, email)
            if target is None:
                typer.echo(f"User {email} not found")
                raise typer.Exit(code=1)
            if target.is_active:
                typer.echo(f"User {email} is already active")
                raise typer.Exit(code=1)
            await enable_user(session, email)
            await session.commit()
        typer.echo(f"Enabled user {email}")

    asyncio.run(_run())


@admin_app.command("delete-user")
def delete_user_command(
    email: str,
    confirm: bool = typer.Option(False, "--confirm", help="Confirm permanent deletion"),
) -> None:
    """Permanently delete a user and all their data."""
    import asyncio

    if not confirm:
        typer.echo("This will permanently delete the user and all their data.")
        typer.echo("Run with --confirm to proceed.")
        raise typer.Exit(code=1)

    async def _run() -> None:
        _, _, session_factory = await _get_runtime()
        async with session_factory() as session:
            target = await get_user(session, email)
            if target is None:
                typer.echo(f"User {email} not found")
                raise typer.Exit(code=1)
            if target.role == "admin":
                admin_count = await count_admins(session)
                if admin_count <= 1:
                    typer.echo("Cannot delete the last admin user")
                    raise typer.Exit(code=1)
            await delete_user_cascade(session, email)
            await session.commit()
        typer.echo(f"Deleted user {email} and all associated data")

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
