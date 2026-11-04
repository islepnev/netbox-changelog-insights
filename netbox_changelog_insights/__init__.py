from netbox.plugins import PluginConfig

__version__ = '0.1.0'


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
    }


config = ChangelogInsightsConfig
