"""mock_server HTTP 客户端（U5）：重试 + 指数退避 + 启动探活。"""
from __future__ import annotations

import asyncio
import logging

import httpx

from . import config

logger = logging.getLogger(__name__)


class MockClient:
    def __init__(self, base_url: str = None):
        self.base_url = (base_url or config.MOCK_SERVER_URL).rstrip("/")
        self._client = httpx.AsyncClient(timeout=config.HTTP_TIMEOUT_S)

    async def close(self):
        await self._client.aclose()

    async def get(self, path: str, params: dict = None, retries: int = 3):
        """GET 带指数退避重试；全部失败返回 None（调用方跳过本轮，不崩溃）。"""
        delay = 1.0
        for attempt in range(retries):
            try:
                resp = await self._client.get(f"{self.base_url}{path}", params=params)
                resp.raise_for_status()
                return resp.json()
            except Exception as e:  # noqa: BLE001
                if attempt == retries - 1:
                    logger.warning("GET %s 失败（已重试 %d 次）: %s", path, retries, e)
                    return None
                await asyncio.sleep(delay)
                delay *= 2
        return None

    async def wait_ready(self, max_wait_s: int = 120) -> bool:
        """启动探活：mock_server 未就绪时退避等待（KTD 启动顺序无关）。"""
        waited, delay = 0.0, 1.0
        while waited < max_wait_s:
            try:
                resp = await self._client.get(f"{self.base_url}/health", timeout=5.0)
                if resp.status_code == 200:
                    logger.info("mock_server 就绪: %s", resp.json())
                    return True
            except Exception:  # noqa: BLE001
                pass
            logger.info("等待 mock_server 就绪... (%.0fs)", waited)
            await asyncio.sleep(delay)
            waited += delay
            delay = min(delay * 2, 10.0)
        logger.error("mock_server 在 %ss 内未就绪", max_wait_s)
        return False

    # --- 契约 A 数据平面 ---

    async def list_metrics(self):
        return await self.get("/cms/ListMetrics")

    async def describe_metric_list(self, namespace: str, metric: str,
                                   start_ms: int = None, end_ms: int = None):
        params = {"Namespace": namespace, "MetricName": metric}
        if start_ms is not None:
            params["StartTime"] = start_ms
        if end_ms is not None:
            params["EndTime"] = end_ms
        return await self.get("/cms/DescribeMetricList", params)

    async def get_logs(self, logstore: str, from_s: int = None, offset: int = 0,
                       lines: int = None):
        params = {"logstore": logstore, "offset": offset, "lines": lines or config.PAGE_SIZE}
        if from_s is not None:
            params["from"] = from_s
        return await self.get("/sls/GetLogs", params)

    async def k8s_resources(self):
        return await self.get("/k8s/resources")

    async def realtime_metrics(self):
        return await self.get("/realtime/metrics")
