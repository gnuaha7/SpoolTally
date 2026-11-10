from fastapi import FastAPI

from spooltally.api.routers import (
    analytics,
    assets,
    catalog,
    export,
    inventory_queries,
    local_catalog,
    locations,
    metrics,
    refresh,
    restore,
    schemas,
    spool_actions,
    spools,
)

PRODUCT_ROUTERS = (
    catalog.router,
    schemas.router,
    assets.router,
    refresh.router,
    local_catalog.router,
    spools.router,
    spool_actions.router,
    inventory_queries.router,
    locations.router,
    export.router,
    restore.router,
    analytics.router,
)


def register_routers(app: FastAPI) -> None:
    for router in PRODUCT_ROUTERS:
        app.include_router(router, prefix="/api/v1")
    app.include_router(metrics.router)
