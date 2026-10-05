from __future__ import annotations

from dataclasses import dataclass

from .versioning import DSMVersion


@dataclass(frozen=True)
class CompatibilityProfile:
    profile_id: str
    minimum_os: DSMVersion
    require_privilege: bool
    require_icons: bool
    require_payload_checksum: bool
    wizard_uifiles_available: bool
    signing_deprecated: bool
    notes: tuple[str, ...]
    sources: tuple[str, ...]


DSM_7_2_2_PLUS = CompatibilityProfile(
    profile_id="dsm-7.2.2+",
    minimum_os=DSMVersion.parse("7.2-72806"),
    require_privilege=True,
    require_icons=True,
    require_payload_checksum=True,
    wizard_uifiles_available=True,
    signing_deprecated=True,
    notes=(
        "DSM 7 packages must explicitly lower privilege through conf/privilege.",
        "precheckstartstop=yes requires prestart and prestop lifecycle handling.",
        "WIZARD_UIFILES is available from DSM 7.2.2.",
        "INFO checksum is a documented MD5 of package.tgz and is emitted defensively for later DSM manual-install compatibility.",
        "SPK signing is deprecated after DSM 7.0.",
        "Future DSM releases are not automatically behavior-qualified; this profile is a known 7.2.2 baseline with no os_max_ver ceiling.",
    ),
    sources=(
        "https://help.synology.com/developer-guide/",
        "https://help.synology.com/developer-guide/synology_package/introduction.html",
        "https://help.synology.com/developer-guide/synology_package/scripts.html",
        "https://help.synology.com/developer-guide/synology_package/conf.html",
        "https://help.synology.com/developer-guide/privilege/privilege_config.html",
        "https://help.synology.com/developer-guide/toolkit/pack_stage.html",
    ),
)

_PROFILES = {DSM_7_2_2_PLUS.profile_id: DSM_7_2_2_PLUS}


def get_profile(profile_id: str) -> CompatibilityProfile:
    try:
        return _PROFILES[profile_id]
    except KeyError as exc:
        raise ValueError(
            f"unknown compatibility profile {profile_id!r}; available: {', '.join(sorted(_PROFILES))}"
        ) from exc


def all_profiles() -> tuple[CompatibilityProfile, ...]:
    return tuple(_PROFILES[key] for key in sorted(_PROFILES))
