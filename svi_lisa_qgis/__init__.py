def classFactory(iface):
    from .svi_lisa import SviLisaPlugin
    return SviLisaPlugin(iface)
