"""Alembic environment: every migration runs as app_owner.

app_owner owns each table it creates, which is what makes FORCE ROW LEVEL SECURITY bind the owner
too. Migrations are hand-written SQL, so there is no metadata to autogenerate from.
"""

import sqlalchemy as sa
from alembic import context

from app.db import make_engine, role_url

if context.is_offline_mode():
    # `alembic upgrade head --sql` prints the SQL without connecting.
    context.configure(url=role_url("app_owner"), literal_binds=True)
    with context.begin_transaction():
        context.run_migrations()
else:
    engine = make_engine(role_url("app_owner"), poolclass=sa.pool.NullPool)
    try:
        with engine.connect() as connection:
            context.configure(connection=connection)
            with context.begin_transaction():
                context.run_migrations()
    finally:
        engine.dispose()
