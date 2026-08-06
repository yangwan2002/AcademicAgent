"""能力感知的 ProviderRegistry（A2c-2 / 任务 2，design D1）。

按角色把检索源分三组（SEARCH / VERIFY / DISCOVERY），构造时**校验声明角色与实际能力
一致**（复用 A1 ``sources.SourceProfile`` 与能力协议）。新增源只需登记 provider + profile，
不改 Loop / 配额 / Gate（Req 11.5）。

不 import ``tasking``、不读全局 config（组合层据 ``config.literature_source_priority``
排序后注入）。
"""

from __future__ import annotations

from dataclasses import dataclass

from paper_agent.literature.sources import (
    ProviderRole,
    SourceProfile,
    profile_for,
    verification_authority,
)
from paper_agent.providers.retrieval.capabilities import (
    DiscoveryProvider,
    SearchProvider,
    VerifyProvider,
)


@dataclass(frozen=True)
class NamedProvider:
    """带名字与能力档案的 provider（name 用于事件 source、profile 判权限）。"""

    name: str
    provider: object
    profile: SourceProfile


class RegistryError(ValueError):
    """声明角色与实际能力不一致（构造时拒绝，不静默接受）。"""


def named(name: str, provider: object) -> NamedProvider:
    """据 A1 profile 表构造 NamedProvider（未知源得最保守兜底档案）。"""
    return NamedProvider(name=name, provider=provider, profile=profile_for(name))


class ProviderRegistry:
    def __init__(
        self,
        *,
        search_sources: tuple[NamedProvider, ...] = (),
        verify_sources: tuple[NamedProvider, ...] = (),
        discovery_sources: tuple[NamedProvider, ...] = (),
    ) -> None:
        self.search_sources = tuple(search_sources)
        self.verify_sources = tuple(verify_sources)
        self.discovery_sources = tuple(discovery_sources)
        self._validate()

    def _validate(self) -> None:
        for np in self.search_sources:
            if ProviderRole.SEARCH not in np.profile.roles:
                raise RegistryError(f"{np.name}: profile 未声明 SEARCH 角色")
            if not isinstance(np.provider, SearchProvider):
                raise RegistryError(f"{np.name}: 不符合 SearchProvider 协议")
        for np in self.verify_sources:
            if ProviderRole.VERIFY not in np.profile.roles:
                raise RegistryError(f"{np.name}: profile 未声明 VERIFY 角色")
            if not isinstance(np.provider, VerifyProvider):
                raise RegistryError(f"{np.name}: 不符合 VerifyProvider 协议")
        for np in self.discovery_sources:
            if ProviderRole.DISCOVER not in np.profile.roles:
                raise RegistryError(f"{np.name}: profile 未声明 DISCOVER 角色")
            if not isinstance(np.provider, DiscoveryProvider):
                raise RegistryError(f"{np.name}: 不符合 DiscoveryProvider 协议")

    def verify_providers_for(self, id_type: str) -> tuple[tuple[str, object], ...]:
        """给出可核验某标识符类型的源 (name, provider)，final 优先、corroboration 其次。

        供 A2c-1 CrossVerifier 消费；无核验权（authority=none）的源不列入。
        """
        finals: list[tuple[str, object]] = []
        corrs: list[tuple[str, object]] = []
        for np in self.verify_sources:
            authority = verification_authority(np.name, id_type)
            if authority == "final":
                finals.append((np.name, np.provider))
            elif authority == "corroboration":
                corrs.append((np.name, np.provider))
        return tuple(finals + corrs)

    def verify_pairs(self) -> tuple[tuple[str, object], ...]:
        """全部核验源 (name, provider)，按注入顺序（供 CrossVerifier 一次性传入）。"""
        return tuple((np.name, np.provider) for np in self.verify_sources)


__all__ = [
    "NamedProvider",
    "ProviderRegistry",
    "RegistryError",
    "named",
]
