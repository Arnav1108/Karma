from enum import Enum


class ComponentSlot(str, Enum):
    gpu = "gpu"
    cpu = "cpu"
    ram = "ram"
    storage = "storage"
    motherboard = "motherboard"
    psu = "psu"
    case = "case"
    cooler = "cooler"
    fans = "fans"
