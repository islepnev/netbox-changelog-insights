from netbox.plugins import PluginConfig

__version__ = '0.5.0'


class ChangelogInsightsConfig(PluginConfig):
    name = 'netbox_changelog_insights'
    verbose_name = 'Changelog Insights'
    description = 'Insights into the changelog: who changed what, when and how much'
    version = __version__
    author = 'islepnev'
    base_url = 'changelog-insights'
    min_version = '4.6.0'
    default_settings = {
        # Name of the user group whose members are automation accounts. None: no split by actor kind.
        'automation_group': None,
        # Cache lifetime of aggregates, in seconds. 0 disables the cache.
        'cache_ttl': 300,
        # Chains: longest gap between change sets on one device or VM, in hours.
        'chain_gap_hours': 7 * 24,
        # Chains: longest gap that a change set of an automation account may bridge, in hours.
        'automation_gap_hours': 24,
        # Longest period of a page, in days; a longer period keeps its last days. None: no limit.
        'max_period_days': None,
    }


config = ChangelogInsightsConfig
