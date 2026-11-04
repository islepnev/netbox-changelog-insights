from netbox.plugins import PluginMenu, PluginMenuItem

PERMISSIONS = ['core.view_objectchange']

menu = PluginMenu(
    label='Insights',
    icon_class='mdi mdi-chart-timeline-variant',
    groups=(
        ('Changelog', (
            PluginMenuItem(
                link='plugins:netbox_changelog_insights:pulse',
                link_text='Pulse',
                permissions=PERMISSIONS,
            ),
            PluginMenuItem(
                link='plugins:netbox_changelog_insights:contributors',
                link_text='Contributors',
                permissions=PERMISSIONS,
            ),
            PluginMenuItem(
                link='plugins:netbox_changelog_insights:activity',
                link_text='Activity',
                permissions=PERMISSIONS,
            ),
            PluginMenuItem(
                link='plugins:netbox_changelog_insights:hotspots',
                link_text='Hotspots',
                permissions=PERMISSIONS,
            ),
            PluginMenuItem(
                link='plugins:netbox_changelog_insights:chains',
                link_text='Chains',
                permissions=PERMISSIONS,
            ),
        )),
    ),
)
