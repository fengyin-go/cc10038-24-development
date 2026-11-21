"""业务路由汇总：所有模块共用一套资源路由，由领域服务完成分派。"""
from __future__ import annotations

from app.routers import resource, pipeline_router, intake_router

# 固定前缀的接收/流水线路由放在通用资源路由之前，避免 /api/intake/files、
# /api/pipeline/snapshot 被 /api/{module}/{entry_id} 当成整数参数抢先匹配。
ROUTERS = [intake_router, pipeline_router, resource]
