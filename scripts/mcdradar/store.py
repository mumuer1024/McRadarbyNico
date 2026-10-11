"""门店上下文与默认门店偏好。

storeCode 是所有餐品/价格/下单类工具的必填项，因此门店上下文必须前置。
只持久化门店编码与名称，**不保存地址、坐标等敏感信息**。
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
from pathlib import Path

from .models import StoreInfo
from .state import StateStore

__all__ = ["DEFAULT_STORE_KEY", "normalize_stores", "save_default_store", "load_default_store", "clear_default_store"]

DEFAULT_STORE_KEY = "default_store"


@dataclass
class StoreOption:
    store_code: str
    store_name: str
    address: str = ""
    business_status: bool | None = None
    distance_m: int | None = None
    be_code: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


def normalize_stores(raw: dict) -> list[StoreOption]:
    data = raw.get("data") if isinstance(raw, dict) else None
    if isinstance(data, dict):
        rows = data.get("list") or data.get("stores") or []
    elif isinstance(data, list):
        rows = data
    else:
        rows = []
    out = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        out.append(StoreOption(
            store_code=str(r.get("storeCode") or ""),
            store_name=str(r.get("storeName") or ""),
            address=str(r.get("address") or ""),
            business_status=r.get("businessStatus"),
            distance_m=r.get("distance"),
            be_code=str(r.get("beCode") or ""),
        ))
    return [o for o in out if o.store_code]


def save_default_store(store: StateStore, info: StoreInfo) -> None:
    store.write(DEFAULT_STORE_KEY, {
        "store_code": info.store_code,
        "store_name": info.store_name,
        "be_type": info.be_type,
        "be_code": info.be_code,
        "order_type": info.order_type,
    })


def load_default_store(store: StateStore) -> StoreInfo | None:
    d = store.read(DEFAULT_STORE_KEY)
    if not d or not d.get("store_code"):
        return None
    return StoreInfo(
        store_code=d["store_code"],
        store_name=d.get("store_name", ""),
        be_type=int(d.get("be_type", 1)),
        be_code=d.get("be_code", ""),
        order_type=int(d.get("order_type", 1)),
    )


def clear_default_store(store: StateStore) -> None:
    store.delete(DEFAULT_STORE_KEY)
