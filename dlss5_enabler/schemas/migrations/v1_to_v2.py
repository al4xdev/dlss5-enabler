from copy import deepcopy


def migrate(data: dict[str, object]) -> dict[str, object]:
    migrated = deepcopy(data)
    if migrated.get("install_options") is None:
        migrated["install_options"] = {
            "lumenite": migrated.get("lumenite_installed", True),
            "d3d9": migrated.get("d3d9_translate", False),
            "opengl": migrated.get("opengl", False),
            "vulkan_layer": migrated.get("vulkan_layer", False),
        }
    migrated["schema_version"] = 2
    return migrated
