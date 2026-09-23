from __future__ import annotations

from dataclasses import dataclass

from .errors import ConfigurationError


@dataclass(frozen=True, slots=True)
class PlatformRoute:
    platform: str
    match_region: str
    realm: str

    @property
    def league_base(self) -> str:
        return f"https://{self.platform.lower()}.api.riotgames.com"

    @property
    def match_base(self) -> str:
        return f"https://{self.match_region.lower()}.api.riotgames.com"

    @property
    def realm_url(self) -> str:
        return f"https://ddragon.leagueoflegends.com/realms/{self.realm}.json"


# Riot League-V4 is platform-routed and Match-V5 is region-routed. Realm names
# follow Data Dragon's separate naming scheme. Keep the mapping explicit so a
# platform cannot accidentally query a different region's match history.
PLATFORM_ROUTES: dict[str, PlatformRoute] = {
    route.platform: route
    for route in (
        PlatformRoute("KR", "ASIA", "kr"),
        PlatformRoute("JP1", "ASIA", "jp"),
        PlatformRoute("NA1", "AMERICAS", "na"),
        PlatformRoute("BR1", "AMERICAS", "br"),
        PlatformRoute("LA1", "AMERICAS", "lan"),
        PlatformRoute("LA2", "AMERICAS", "las"),
        PlatformRoute("EUW1", "EUROPE", "euw"),
        PlatformRoute("EUN1", "EUROPE", "eune"),
        PlatformRoute("TR1", "EUROPE", "tr"),
        PlatformRoute("RU", "EUROPE", "ru"),
        PlatformRoute("ME1", "EUROPE", "me"),
        PlatformRoute("OC1", "SEA", "oce"),
        PlatformRoute("PH2", "SEA", "ph"),
        PlatformRoute("SG2", "SEA", "sg"),
        PlatformRoute("TH2", "SEA", "th"),
        PlatformRoute("TW2", "SEA", "tw"),
        PlatformRoute("VN2", "SEA", "vn"),
    )
}
SUPPORTED_PLATFORMS = tuple(PLATFORM_ROUTES)


def platform_route(platform: str) -> PlatformRoute:
    normalized = platform.strip().upper()
    try:
        return PLATFORM_ROUTES[normalized]
    except KeyError as exc:
        raise ConfigurationError(
            "PLATFORM_UNSUPPORTED",
            f"Unsupported international platform {platform!r}; choose one of "
            + ", ".join(SUPPORTED_PLATFORMS),
        ) from exc
