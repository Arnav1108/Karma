-- data/catalog/seed_expansion.sql
-- APPEND to data/catalog/seed.sql (before COMMIT;) OR run standalone inside a txn.
-- Karma Advisor catalog expansion — current + still-widely-sold gen, 4 sockets, DDR4/5.
-- Prices: existing-catalog ladder + 2026 Indian street anchors (volatile: GDDR/DDR shortage).
BEGIN;

-- GPU
INSERT INTO catalog (product_id, category, name, brand, price_inr, in_stock, specs) VALUES
('gpu-015', 'gpu', 'Suprim X RTX 5090 32G', 'MSI', 235000, TRUE, '{"vram_gb": 32, "tdp_watts": 575, "length_mm": 360, "slot_width": 3.5, "pcie_gen": 5}'),
('gpu-016', 'gpu', 'ROG Astral RTX 5090 32GB OC', 'ASUS', 258000, TRUE, '{"vram_gb": 32, "tdp_watts": 575, "length_mm": 358, "slot_width": 4.0, "pcie_gen": 5}'),
('gpu-039', 'gpu', 'GeForce RTX 5090 Gaming OC 32G', 'Gigabyte', 245000, TRUE, '{"vram_gb": 32, "tdp_watts": 575, "length_mm": 355, "slot_width": 3.5, "pcie_gen": 5}'),
('gpu-040', 'gpu', 'RTX 5090 Solid OC 32GB', 'Zotac', 228000, TRUE, '{"vram_gb": 32, "tdp_watts": 575, "length_mm": 348, "slot_width": 3.0, "pcie_gen": 5}'),
('gpu-017', 'gpu', 'ROG Strix RTX 5070 Ti 16GB OC', 'ASUS', 85000, TRUE, '{"vram_gb": 16, "tdp_watts": 300, "length_mm": 330, "slot_width": 3.0, "pcie_gen": 5}'),
('gpu-018', 'gpu', 'GeForce RTX 5070 Ti Gaming OC 16G', 'Gigabyte', 82000, TRUE, '{"vram_gb": 16, "tdp_watts": 300, "length_mm": 336, "slot_width": 3.0, "pcie_gen": 5}'),
('gpu-019', 'gpu', 'RTX 5060 Ti 16G Ventus 3X OC', 'MSI', 42000, TRUE, '{"vram_gb": 16, "tdp_watts": 180, "length_mm": 242, "slot_width": 2.0, "pcie_gen": 5}'),
('gpu-020', 'gpu', 'Dual RTX 5060 Ti 8GB OC', 'ASUS', 36000, TRUE, '{"vram_gb": 8, "tdp_watts": 180, "length_mm": 228, "slot_width": 2.0, "pcie_gen": 5}'),
('gpu-021', 'gpu', 'RTX 5060 Ventus 2X 8G OC', 'MSI', 32000, TRUE, '{"vram_gb": 8, "tdp_watts": 145, "length_mm": 200, "slot_width": 2.0, "pcie_gen": 5}'),
('gpu-022', 'gpu', 'GeForce RTX 5070 Windforce OC 12G', 'Gigabyte', 52000, TRUE, '{"vram_gb": 12, "tdp_watts": 250, "length_mm": 300, "slot_width": 2.5, "pcie_gen": 5}'),
('gpu-023', 'gpu', 'ROG Strix RTX 5080 16GB OC', 'ASUS', 118000, TRUE, '{"vram_gb": 16, "tdp_watts": 360, "length_mm": 358, "slot_width": 3.5, "pcie_gen": 5}'),
('gpu-024', 'gpu', 'Twin Edge RTX 4060 8GB', 'Zotac', 26000, TRUE, '{"vram_gb": 8, "tdp_watts": 115, "length_mm": 225, "slot_width": 2.0, "pcie_gen": 4}'),
('gpu-025', 'gpu', 'Prime RTX 4070 Super 12GB OC', 'ASUS', 57000, TRUE, '{"vram_gb": 12, "tdp_watts": 220, "length_mm": 305, "slot_width": 2.5, "pcie_gen": 4}'),
('gpu-026', 'gpu', 'RTX 3060 Gaming OC 12G', 'Gigabyte', 29000, TRUE, '{"vram_gb": 12, "tdp_watts": 170, "length_mm": 282, "slot_width": 2.0, "pcie_gen": 4}'),
('gpu-027', 'gpu', 'RTX 3050 Eagle OC 8G', 'Gigabyte', 20000, TRUE, '{"vram_gb": 8, "tdp_watts": 130, "length_mm": 212, "slot_width": 2.0, "pcie_gen": 4}'),
('gpu-028', 'gpu', 'Hellhound RX 9070 XT 16GB', 'PowerColor', 51000, TRUE, '{"vram_gb": 16, "tdp_watts": 304, "length_mm": 330, "slot_width": 3.0, "pcie_gen": 5}'),
('gpu-029', 'gpu', 'PULSE RX 9060 XT 16GB', 'Sapphire', 35000, TRUE, '{"vram_gb": 16, "tdp_watts": 150, "length_mm": 250, "slot_width": 2.0, "pcie_gen": 5}'),
('gpu-030', 'gpu', 'RX 9060 XT Gaming OC 8G', 'Gigabyte', 30000, TRUE, '{"vram_gb": 8, "tdp_watts": 150, "length_mm": 248, "slot_width": 2.0, "pcie_gen": 5}'),
('gpu-031', 'gpu', 'Hellhound RX 7900 XT 20GB', 'PowerColor', 70000, TRUE, '{"vram_gb": 20, "tdp_watts": 315, "length_mm": 320, "slot_width": 2.5, "pcie_gen": 4}'),
('gpu-032', 'gpu', 'PULSE RX 7700 XT 12GB', 'Sapphire', 38000, TRUE, '{"vram_gb": 12, "tdp_watts": 245, "length_mm": 280, "slot_width": 2.5, "pcie_gen": 4}'),
('gpu-033', 'gpu', 'RX 7600 Eagle 8G', 'Gigabyte', 22000, TRUE, '{"vram_gb": 8, "tdp_watts": 165, "length_mm": 228, "slot_width": 2.0, "pcie_gen": 4}'),
('gpu-034', 'gpu', 'RX 6600 Eagle 8G', 'Gigabyte', 18000, TRUE, '{"vram_gb": 8, "tdp_watts": 132, "length_mm": 232, "slot_width": 2.0, "pcie_gen": 4}'),
('gpu-035', 'gpu', 'PULSE RX 6650 XT 8GB', 'Sapphire', 20000, TRUE, '{"vram_gb": 8, "tdp_watts": 180, "length_mm": 254, "slot_width": 2.0, "pcie_gen": 4}'),
('gpu-036', 'gpu', 'RX 6750 XT Challenger 12GB', 'ASRock', 28000, TRUE, '{"vram_gb": 12, "tdp_watts": 250, "length_mm": 303, "slot_width": 2.5, "pcie_gen": 4}'),
('gpu-037', 'gpu', 'RX 7800 XT Gaming OC 16G', 'Gigabyte', 43000, TRUE, '{"vram_gb": 16, "tdp_watts": 263, "length_mm": 302, "slot_width": 2.5, "pcie_gen": 4}'),
('gpu-038', 'gpu', 'PULSE RX 9070 16GB', 'Sapphire', 45000, TRUE, '{"vram_gb": 16, "tdp_watts": 220, "length_mm": 320, "slot_width": 2.5, "pcie_gen": 5}');

-- CPU
INSERT INTO catalog (product_id, category, name, brand, price_inr, in_stock, specs) VALUES
('cpu-013', 'cpu', 'Core i3-12100F', 'Intel', 8000, TRUE, '{"socket": "LGA1700", "tdp_watts": 58, "cores": 4, "threads": 8, "base_ghz": 3.3, "boost_ghz": 4.3, "has_igpu": false}'),
('cpu-014', 'cpu', 'Core i5-12400F', 'Intel', 11000, TRUE, '{"socket": "LGA1700", "tdp_watts": 65, "cores": 6, "threads": 12, "base_ghz": 2.5, "boost_ghz": 4.4, "has_igpu": false}'),
('cpu-015', 'cpu', 'Core i5-13400F', 'Intel', 17500, TRUE, '{"socket": "LGA1700", "tdp_watts": 65, "cores": 10, "threads": 16, "base_ghz": 2.5, "boost_ghz": 4.6, "has_igpu": false}'),
('cpu-016', 'cpu', 'Core i5-13600K', 'Intel', 24000, TRUE, '{"socket": "LGA1700", "tdp_watts": 125, "cores": 14, "threads": 20, "base_ghz": 3.5, "boost_ghz": 5.1, "has_igpu": true}'),
('cpu-017', 'cpu', 'Core i7-13700K', 'Intel', 33000, TRUE, '{"socket": "LGA1700", "tdp_watts": 125, "cores": 16, "threads": 24, "base_ghz": 3.4, "boost_ghz": 5.4, "has_igpu": true}'),
('cpu-018', 'cpu', 'Core Ultra 5 225F', 'Intel', 20000, TRUE, '{"socket": "LGA1851", "tdp_watts": 65, "cores": 10, "threads": 10, "base_ghz": 3.3, "boost_ghz": 4.9, "has_igpu": false}'),
('cpu-019', 'cpu', 'Core Ultra 7 265K', 'Intel', 38000, TRUE, '{"socket": "LGA1851", "tdp_watts": 125, "cores": 20, "threads": 20, "base_ghz": 3.9, "boost_ghz": 5.5, "has_igpu": true}'),
('cpu-020', 'cpu', 'Ryzen 5 5500', 'AMD', 8500, TRUE, '{"socket": "AM4", "tdp_watts": 65, "cores": 6, "threads": 12, "base_ghz": 3.6, "boost_ghz": 4.2, "has_igpu": false}'),
('cpu-021', 'cpu', 'Ryzen 5 5600', 'AMD', 10000, TRUE, '{"socket": "AM4", "tdp_watts": 65, "cores": 6, "threads": 12, "base_ghz": 3.5, "boost_ghz": 4.4, "has_igpu": false}'),
('cpu-022', 'cpu', 'Ryzen 7 5700X', 'AMD', 14000, TRUE, '{"socket": "AM4", "tdp_watts": 65, "cores": 8, "threads": 16, "base_ghz": 3.4, "boost_ghz": 4.6, "has_igpu": false}'),
('cpu-023', 'cpu', 'Ryzen 7 5700X3D', 'AMD', 18000, TRUE, '{"socket": "AM4", "tdp_watts": 105, "cores": 8, "threads": 16, "base_ghz": 3.0, "boost_ghz": 4.1, "has_igpu": false}'),
('cpu-024', 'cpu', 'Ryzen 7 5800X3D', 'AMD', 22000, TRUE, '{"socket": "AM4", "tdp_watts": 105, "cores": 8, "threads": 16, "base_ghz": 3.4, "boost_ghz": 4.5, "has_igpu": false}'),
('cpu-025', 'cpu', 'Ryzen 5 7500F', 'AMD', 15000, TRUE, '{"socket": "AM5", "tdp_watts": 65, "cores": 6, "threads": 12, "base_ghz": 3.7, "boost_ghz": 5.0, "has_igpu": false}'),
('cpu-026', 'cpu', 'Ryzen 7 7800X3D', 'AMD', 38000, TRUE, '{"socket": "AM5", "tdp_watts": 120, "cores": 8, "threads": 16, "base_ghz": 4.2, "boost_ghz": 5.0, "has_igpu": true}'),
('cpu-027', 'cpu', 'Ryzen 5 9600X', 'AMD', 22000, TRUE, '{"socket": "AM5", "tdp_watts": 65, "cores": 6, "threads": 12, "base_ghz": 3.9, "boost_ghz": 5.4, "has_igpu": true}'),
('cpu-028', 'cpu', 'Ryzen 7 9700X', 'AMD', 31000, TRUE, '{"socket": "AM5", "tdp_watts": 65, "cores": 8, "threads": 16, "base_ghz": 3.8, "boost_ghz": 5.5, "has_igpu": true}'),
('cpu-029', 'cpu', 'Ryzen 7 9800X3D', 'AMD', 45000, TRUE, '{"socket": "AM5", "tdp_watts": 120, "cores": 8, "threads": 16, "base_ghz": 4.7, "boost_ghz": 5.2, "has_igpu": true}'),
('cpu-030', 'cpu', 'Ryzen 9 9900X', 'AMD', 40000, TRUE, '{"socket": "AM5", "tdp_watts": 120, "cores": 12, "threads": 24, "base_ghz": 4.4, "boost_ghz": 5.6, "has_igpu": true}'),
('cpu-031', 'cpu', 'Ryzen 9 9950X', 'AMD', 58000, TRUE, '{"socket": "AM5", "tdp_watts": 170, "cores": 16, "threads": 32, "base_ghz": 4.3, "boost_ghz": 5.7, "has_igpu": true}');

-- Motherboard
INSERT INTO catalog (product_id, category, name, brand, price_inr, in_stock, specs) VALUES
('mb-013', 'motherboard', 'B760M Gaming Plus WiFi DDR5', 'MSI', 14000, TRUE, '{"socket": "LGA1700", "chipset": "B760", "form_factor": "mATX", "max_ram_gb": 192, "ram_slots": 4, "pcie_slots": 1, "ddr_type": 5}'),
('mb-014', 'motherboard', 'Z790 Aorus Elite AX', 'Gigabyte', 32000, TRUE, '{"socket": "LGA1700", "chipset": "Z790", "form_factor": "ATX", "max_ram_gb": 192, "ram_slots": 4, "pcie_slots": 3, "ddr_type": 5}'),
('mb-015', 'motherboard', 'PRO H770-P WiFi DDR5', 'MSI', 18000, TRUE, '{"socket": "LGA1700", "chipset": "H770", "form_factor": "ATX", "max_ram_gb": 192, "ram_slots": 4, "pcie_slots": 2, "ddr_type": 5}'),
('mb-016', 'motherboard', 'PRO B860M-A WiFi', 'MSI', 18000, TRUE, '{"socket": "LGA1851", "chipset": "B860", "form_factor": "mATX", "max_ram_gb": 256, "ram_slots": 4, "pcie_slots": 1, "ddr_type": 5}'),
('mb-017', 'motherboard', 'TUF Gaming Z890-Plus WiFi', 'ASUS', 26000, TRUE, '{"socket": "LGA1851", "chipset": "Z890", "form_factor": "ATX", "max_ram_gb": 256, "ram_slots": 4, "pcie_slots": 2, "ddr_type": 5}'),
('mb-018', 'motherboard', 'A620M-E', 'ASRock', 9000, TRUE, '{"socket": "AM5", "chipset": "A620", "form_factor": "mATX", "max_ram_gb": 128, "ram_slots": 2, "pcie_slots": 1, "ddr_type": 5}'),
('mb-019', 'motherboard', 'B650E Aorus Elite X AX', 'Gigabyte', 22000, TRUE, '{"socket": "AM5", "chipset": "B650E", "form_factor": "ATX", "max_ram_gb": 256, "ram_slots": 4, "pcie_slots": 2, "ddr_type": 5}'),
('mb-020', 'motherboard', 'PRO X670-P WiFi', 'MSI', 24000, TRUE, '{"socket": "AM5", "chipset": "X670", "form_factor": "ATX", "max_ram_gb": 256, "ram_slots": 4, "pcie_slots": 2, "ddr_type": 5}'),
('mb-021', 'motherboard', 'B550 Aorus Elite AX V2', 'Gigabyte', 9500, TRUE, '{"socket": "AM4", "chipset": "B550", "form_factor": "ATX", "max_ram_gb": 128, "ram_slots": 4, "pcie_slots": 2, "ddr_type": 4}'),
('mb-022', 'motherboard', 'B550M DS3H', 'Gigabyte', 7500, TRUE, '{"socket": "AM4", "chipset": "B550", "form_factor": "mATX", "max_ram_gb": 128, "ram_slots": 4, "pcie_slots": 1, "ddr_type": 4}'),
('mb-023', 'motherboard', 'A520M-HDV', 'ASRock', 5500, TRUE, '{"socket": "AM4", "chipset": "A520", "form_factor": "mATX", "max_ram_gb": 64, "ram_slots": 2, "pcie_slots": 1, "ddr_type": 4}'),
('mb-024', 'motherboard', 'X570 Steel Legend WiFi ax', 'ASRock', 14000, TRUE, '{"socket": "AM4", "chipset": "X570", "form_factor": "ATX", "max_ram_gb": 128, "ram_slots": 4, "pcie_slots": 2, "ddr_type": 4}'),
('mb-025', 'motherboard', 'B760 Gaming X DDR4', 'Gigabyte', 11000, TRUE, '{"socket": "LGA1700", "chipset": "B760", "form_factor": "ATX", "max_ram_gb": 128, "ram_slots": 4, "pcie_slots": 2, "ddr_type": 4}');

-- RAM
INSERT INTO catalog (product_id, category, name, brand, price_inr, in_stock, specs) VALUES
('ram-012', 'ram', 'Value DDR4-3200 8GB', 'Crucial', 1800, TRUE, '{"capacity_gb": 8, "speed_mhz": 3200, "ddr_gen": 4, "kit_count": 1}'),
('ram-013', 'ram', 'FURY Beast DDR5-5600 16GB Kit', 'Kingston', 5500, TRUE, '{"capacity_gb": 16, "speed_mhz": 5600, "ddr_gen": 5, "kit_count": 2}'),
('ram-014', 'ram', 'Vengeance DDR5-6000 32GB Kit', 'Corsair', 11500, TRUE, '{"capacity_gb": 32, "speed_mhz": 6000, "ddr_gen": 5, "kit_count": 2}'),
('ram-015', 'ram', 'Trident Z5 RGB DDR5-6400 32GB Kit', 'G.Skill', 13000, TRUE, '{"capacity_gb": 32, "speed_mhz": 6400, "ddr_gen": 5, "kit_count": 2}'),
('ram-016', 'ram', 'Ripjaws V DDR4-3200 64GB Kit', 'G.Skill', 11000, TRUE, '{"capacity_gb": 64, "speed_mhz": 3200, "ddr_gen": 4, "kit_count": 2}'),
('ram-017', 'ram', 'Vengeance DDR5-5200 48GB Kit', 'Corsair', 15000, TRUE, '{"capacity_gb": 48, "speed_mhz": 5200, "ddr_gen": 5, "kit_count": 2}'),
('ram-018', 'ram', 'Trident Z5 DDR5-6000 64GB Kit', 'G.Skill', 21000, TRUE, '{"capacity_gb": 64, "speed_mhz": 6000, "ddr_gen": 5, "kit_count": 2}');

-- Storage
INSERT INTO catalog (product_id, category, name, brand, price_inr, in_stock, specs) VALUES
('storage-013', 'storage', 'NV3 500GB M.2 NVMe', 'Kingston', 3000, TRUE, '{"capacity_gb": 500, "interface": "M.2 NVMe Gen4", "read_mbps": 5000, "write_mbps": 3000}'),
('storage-014', 'storage', '990 EVO Plus 1TB Gen5', 'Samsung', 11000, TRUE, '{"capacity_gb": 1000, "interface": "M.2 NVMe Gen5", "read_mbps": 9000, "write_mbps": 7500}'),
('storage-015', 'storage', 'Black SN850X 4TB M.2 NVMe', 'WD', 26000, TRUE, '{"capacity_gb": 4000, "interface": "M.2 NVMe Gen4", "read_mbps": 7300, "write_mbps": 6600}'),
('storage-016', 'storage', 'NV2 2TB M.2 NVMe', 'Kingston', 11000, TRUE, '{"capacity_gb": 2000, "interface": "M.2 NVMe Gen4", "read_mbps": 3500, "write_mbps": 2800}');

-- PSU
INSERT INTO catalog (product_id, category, name, brand, price_inr, in_stock, specs) VALUES
('psu-012', 'psu', 'CV550 550W 80+ Bronze', 'Corsair', 4200, TRUE, '{"wattage": 550, "efficiency_rating": "80+ Bronze", "modular": "non"}'),
('psu-013', 'psu', 'MWE 750W Gold V2', 'Cooler Master', 8500, TRUE, '{"wattage": 750, "efficiency_rating": "80+ Gold", "modular": "full"}'),
('psu-014', 'psu', 'RM850e 80+ Gold', 'Corsair', 10500, TRUE, '{"wattage": 850, "efficiency_rating": "80+ Gold", "modular": "full"}'),
('psu-015', 'psu', 'Focus GX-1000 80+ Gold', 'Seasonic', 13000, TRUE, '{"wattage": 1000, "efficiency_rating": "80+ Gold", "modular": "full"}'),
('psu-016', 'psu', 'HX1200 80+ Platinum', 'Corsair', 20000, TRUE, '{"wattage": 1200, "efficiency_rating": "80+ Platinum", "modular": "full"}'),
('psu-017', 'psu', 'Prime PX-1000 80+ Platinum', 'Seasonic', 16000, TRUE, '{"wattage": 1000, "efficiency_rating": "80+ Platinum", "modular": "full"}');

-- Case
INSERT INTO catalog (product_id, category, name, brand, price_inr, in_stock, specs) VALUES
('case-012', 'case', 'MasterBox Q300L mATX', 'Cooler Master', 3500, TRUE, '{"form_factor_support": ["mATX", "ITX"], "max_gpu_length_mm": 360, "max_cooler_height_mm": 159}'),
('case-013', 'case', 'Versa H18 ATX', 'Thermaltake', 5000, TRUE, '{"form_factor_support": ["ATX", "mATX", "ITX"], "max_gpu_length_mm": 350, "max_cooler_height_mm": 155}'),
('case-014', 'case', '4000D Airflow ATX', 'Corsair', 9000, TRUE, '{"form_factor_support": ["ATX", "mATX", "ITX"], "max_gpu_length_mm": 360, "max_cooler_height_mm": 170}'),
('case-015', 'case', 'O11 Vision ATX', 'Lian Li', 18000, TRUE, '{"form_factor_support": ["ATX", "mATX", "ITX"], "max_gpu_length_mm": 455, "max_cooler_height_mm": 167}');

-- Cooler
INSERT INTO catalog (product_id, category, name, brand, price_inr, in_stock, specs) VALUES
('cooler-011', 'cooler', 'Gammaxx 400 V2 Air', 'DeepCool', 1500, TRUE, '{"type": "air", "tdp_support_watts": 150, "height_mm": 155, "radiator_size_mm": null, "socket_compat": ["LGA1700", "LGA1851", "AM4", "AM5"]}'),
('cooler-012', 'cooler', 'Assassin IV Air', 'DeepCool', 3200, TRUE, '{"type": "air", "tdp_support_watts": 270, "height_mm": 164, "radiator_size_mm": null, "socket_compat": ["LGA1700", "LGA1851", "AM4", "AM5"]}'),
('cooler-013', 'cooler', 'iCUE H100i RGB Elite 240mm', 'Corsair', 7500, TRUE, '{"type": "aio", "tdp_support_watts": 250, "height_mm": null, "radiator_size_mm": 240, "socket_compat": ["LGA1700", "LGA1851", "AM4", "AM5"]}'),
('cooler-014', 'cooler', 'Kraken 360 RGB AIO', 'NZXT', 13000, TRUE, '{"type": "aio", "tdp_support_watts": 350, "height_mm": null, "radiator_size_mm": 360, "socket_compat": ["LGA1700", "LGA1851", "AM4", "AM5"]}'),
('cooler-015', 'cooler', 'AK500 Low-Profile Air', 'DeepCool', 4500, TRUE, '{"type": "air", "tdp_support_watts": 220, "height_mm": 112, "radiator_size_mm": null, "socket_compat": ["LGA1700", "LGA1851", "AM4", "AM5"]}');

-- Fans
INSERT INTO catalog (product_id, category, name, brand, price_inr, in_stock, specs) VALUES
('fans-011', 'fans', 'T30 3-Pack 120mm', 'Thermalright', 1800, TRUE, '{"size_mm": 120, "static_pressure": 3.5, "airflow_cfm": 68.0}'),
('fans-012', 'fans', 'AF140 ELITE ARGB 140mm', 'Corsair', 2800, TRUE, '{"size_mm": 140, "static_pressure": 1.9, "airflow_cfm": 82.0}');

COMMIT;