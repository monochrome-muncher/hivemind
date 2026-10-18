"""The expanded multi-domain corpus (see README.md).

The registry merges the five original domains (``tests.eval.domains`` /
``tests.eval.version_pairs``) with their extensions and the eleven new
domains, into:

- ``EXTENDED_DOMAINS`` — a ``tuple[Domain, ...]`` of all sixteen domains,
  for the similarity-threshold harness (``tests.eval.threshold``).
- ``EXTENDED_VERSION_DOMAINS`` — a ``tuple[VersionDomain, ...]`` of all
  sixteen, for the version-grouping tau harness (``tests.eval.versions``).

Each extension module (``data_analysis``, ``software_engineering``,
``system_administration``, ``journalism_social_science``,
``finance_analysis``) carries ``EXTRA_ENTRIES`` / ``EXTRA_PAIRS`` /
``EXTRA_ANSWERABLE`` / ``EXTRA_UNANSWERED``. ``EXTRA_ANSWERABLE`` indices
are zero-based over the extra entries only; this registry adds the original
domain's entry count before use. Each new-domain module carries a complete
``DOMAIN`` / ``VERSIONS`` pair.
"""

from __future__ import annotations

from types import ModuleType

from tests.eval.corpus import (
    blue_team,
    data_analysis,
    data_engineering,
    database_engineering,
    finance_analysis,
    intelligence_analysis,
    journalism_social_science,
    legal_compliance,
    network_operations,
    platform_engineering,
    red_team,
    research_literature,
    software_engineering,
    system_administration,
    vulnerability_research,
    web_search,
)
from tests.eval.domains import CODE, DATA, FINANCE, OPS, RESEARCH, Domain
from tests.eval.version_pairs import (
    CODE_VERSIONS,
    DATA_VERSIONS,
    FINANCE_VERSIONS,
    OPS_VERSIONS,
    RESEARCH_VERSIONS,
    VersionDomain,
)


def _extend(
    domain: Domain, module: ModuleType, version_domain: VersionDomain
) -> tuple[Domain, VersionDomain]:
    """Merge an extension module into an original domain.

    ``module.EXTRA_ANSWERABLE`` indices are zero-based over the extra
    entries; the original entry count is added so they address the merged
    pool.
    """
    offset = len(domain.entries)
    entries = domain.entries + module.EXTRA_ENTRIES
    answerable = domain.answerable + tuple(
        (q, tuple(offset + i for i in idx)) for q, idx in module.EXTRA_ANSWERABLE
    )
    unanswered = domain.unanswered + module.EXTRA_UNANSWERED
    merged = Domain(domain.name, entries, answerable, unanswered)
    versions = VersionDomain(merged, version_domain.pairs + module.EXTRA_PAIRS)
    return merged, versions


# The five original domains, each extended in place.
_EXT_DATA, _VER_DATA = _extend(DATA, data_analysis, DATA_VERSIONS)
_EXT_CODE, _VER_CODE = _extend(CODE, software_engineering, CODE_VERSIONS)
_EXT_OPS, _VER_OPS = _extend(OPS, system_administration, OPS_VERSIONS)
_EXT_RESEARCH, _VER_RESEARCH = _extend(RESEARCH, journalism_social_science, RESEARCH_VERSIONS)
_EXT_FINANCE, _VER_FINANCE = _extend(FINANCE, finance_analysis, FINANCE_VERSIONS)

# The eleven new domains, complete from their modules.
_NEW_DOMAINS: tuple[Domain, ...] = (
    web_search.DOMAIN,
    intelligence_analysis.DOMAIN,
    vulnerability_research.DOMAIN,
    blue_team.DOMAIN,
    red_team.DOMAIN,
    database_engineering.DOMAIN,
    network_operations.DOMAIN,
    data_engineering.DOMAIN,
    platform_engineering.DOMAIN,
    research_literature.DOMAIN,
    legal_compliance.DOMAIN,
)
_NEW_VERSIONS: tuple[VersionDomain, ...] = (
    web_search.VERSIONS,
    intelligence_analysis.VERSIONS,
    vulnerability_research.VERSIONS,
    blue_team.VERSIONS,
    red_team.VERSIONS,
    database_engineering.VERSIONS,
    network_operations.VERSIONS,
    data_engineering.VERSIONS,
    platform_engineering.VERSIONS,
    research_literature.VERSIONS,
    legal_compliance.VERSIONS,
)

# All sixteen, original order first then the new domains.
EXTENDED_DOMAINS: tuple[Domain, ...] = (
    _EXT_DATA,
    _EXT_CODE,
    _EXT_OPS,
    _EXT_RESEARCH,
    _EXT_FINANCE,
    *_NEW_DOMAINS,
)

EXTENDED_VERSION_DOMAINS: tuple[VersionDomain, ...] = (
    _VER_DATA,
    _VER_CODE,
    _VER_OPS,
    _VER_RESEARCH,
    _VER_FINANCE,
    *_NEW_VERSIONS,
)


def _check() -> None:
    names = [d.name for d in EXTENDED_DOMAINS]
    assert len(names) == 16, len(names)
    assert len(set(names)) == 16, "duplicate domain names"
    assert len(EXTENDED_VERSION_DOMAINS) == 16
    for d, v in zip(EXTENDED_DOMAINS, EXTENDED_VERSION_DOMAINS, strict=True):
        assert d.name == v.name, (d.name, v.name)


_check()
