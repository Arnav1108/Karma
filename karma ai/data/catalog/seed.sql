-- data/catalog/seed.sql
-- Karma Advisor product catalog — June 2026 INR pricing
-- Covers all nine ComponentSlot categories with compatibility spec fields.

BEGIN;

CREATE TABLE IF NOT EXISTS catalog (
    product_id  TEXT     PRIMARY KEY,
    category    TEXT     NOT NULL CHECK (category IN (
                            'gpu','cpu','ram','storage',
                            'motherboard','psu','case','cooler','fans')),
    name        TEXT     NOT NULL,
    brand       TEXT     NOT NULL,
    price_inr   INTEGER  NOT NULL CHECK (price_inr > 0),
    in_stock    BOOLEAN  NOT NULL DEFAULT TRUE,
    specs       JSONB    NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_catalog_category_price ON catalog (category, price_inr);
CREATE INDEX IF NOT EXISTS idx_catalog_in_stock       ON catalog (in_stock);

-- ─────────────────────────────────────────────────────────────────────────────
-- GPU  specs: vram_gb, tdp_watts, length_mm, slot_width, pcie_gen
-- ─────────────────────────────────────────────────────────────────────────────
INSERT INTO catalog (product_id, category, name, brand, price_inr, in_stock, specs) VALUES

('gpu-001', 'gpu', 'RTX 4060 Ventus 2X 8G OC',             'MSI',        27500, TRUE,
 '{"vram_gb":8,  "tdp_watts":115, "length_mm":200, "slot_width":2.0, "pcie_gen":4}'),

('gpu-002', 'gpu', 'TUF Gaming RTX 4060 Ti 8GB OC',        'ASUS',       37000, TRUE,
 '{"vram_gb":8,  "tdp_watts":165, "length_mm":305, "slot_width":2.5, "pcie_gen":4}'),

('gpu-003', 'gpu', 'GeForce RTX 4070 Gaming OC 12G',       'Gigabyte',   51000, TRUE,
 '{"vram_gb":12, "tdp_watts":200, "length_mm":327, "slot_width":2.5, "pcie_gen":4}'),

('gpu-004', 'gpu', 'RTX 4070 Super Gaming X Slim 12G',     'MSI',        58000, TRUE,
 '{"vram_gb":12, "tdp_watts":220, "length_mm":336, "slot_width":2.0, "pcie_gen":4}'),

('gpu-005', 'gpu', 'ROG Strix RTX 4070 Ti Super 16GB OC',  'ASUS',       76000, TRUE,
 '{"vram_gb":16, "tdp_watts":285, "length_mm":357, "slot_width":3.0, "pcie_gen":4}'),

('gpu-006', 'gpu', 'RTX 4080 Super Gaming OC 16G',         'Gigabyte',   99000, TRUE,
 '{"vram_gb":16, "tdp_watts":320, "length_mm":348, "slot_width":3.0, "pcie_gen":4}'),

('gpu-007', 'gpu', 'ROG Strix RTX 4090 OC 24GB',           'ASUS',      175000, TRUE,
 '{"vram_gb":24, "tdp_watts":450, "length_mm":358, "slot_width":3.5, "pcie_gen":4}'),

-- out of stock: tests in-stock filter on AMD budget tier
('gpu-008', 'gpu', 'PULSE RX 7600 XT 16G',                 'Sapphire',   26500, FALSE,
 '{"vram_gb":16, "tdp_watts":165, "length_mm":248, "slot_width":2.0, "pcie_gen":4}'),

('gpu-009', 'gpu', 'NITRO+ RX 7800 XT 16GB',               'Sapphire',   42000, TRUE,
 '{"vram_gb":16, "tdp_watts":263, "length_mm":338, "slot_width":2.5, "pcie_gen":4}'),

('gpu-010', 'gpu', 'Red Devil RX 7900 XTX 24GB',           'PowerColor', 83000, TRUE,
 '{"vram_gb":24, "tdp_watts":355, "length_mm":356, "slot_width":3.0, "pcie_gen":4}'),

('gpu-011', 'gpu', 'TUF Gaming RTX 5070 12GB OC',          'ASUS',       54000, TRUE,
 '{"vram_gb":12, "tdp_watts":250, "length_mm":310, "slot_width":2.5, "pcie_gen":5}'),

('gpu-012', 'gpu', 'RTX 5080 16G Gaming X Trio',           'MSI',       112000, TRUE,
 '{"vram_gb":16, "tdp_watts":360, "length_mm":352, "slot_width":3.0, "pcie_gen":5}'),

('gpu-013', 'gpu', 'NITRO+ RX 9070 XT 16GB',               'Sapphire',   49000, TRUE,
 '{"vram_gb":16, "tdp_watts":275, "length_mm":330, "slot_width":2.5, "pcie_gen":5}'),

('gpu-014', 'gpu', 'RX 9070 Gaming OC 16G',                'Gigabyte',   44000, TRUE,
 '{"vram_gb":16, "tdp_watts":220, "length_mm":318, "slot_width":2.5, "pcie_gen":5}');


-- ─────────────────────────────────────────────────────────────────────────────
-- CPU  specs: socket, tdp_watts, cores, threads, base_ghz, boost_ghz, has_igpu
-- ─────────────────────────────────────────────────────────────────────────────
INSERT INTO catalog (product_id, category, name, brand, price_inr, in_stock, specs) VALUES

('cpu-001', 'cpu', 'Core i3-14100F',      'Intel',  9000, TRUE,
 '{"socket":"LGA1700","tdp_watts":58,  "cores":4,  "threads":8,  "base_ghz":3.5,"boost_ghz":4.7,"has_igpu":false}'),

('cpu-002', 'cpu', 'Core i5-14400F',      'Intel', 16500, TRUE,
 '{"socket":"LGA1700","tdp_watts":65,  "cores":10, "threads":16, "base_ghz":2.5,"boost_ghz":4.7,"has_igpu":false}'),

('cpu-003', 'cpu', 'Core i5-14600K',      'Intel', 24000, TRUE,
 '{"socket":"LGA1700","tdp_watts":125, "cores":14, "threads":20, "base_ghz":3.5,"boost_ghz":5.3,"has_igpu":true}'),

('cpu-004', 'cpu', 'Core i7-14700K',      'Intel', 36000, TRUE,
 '{"socket":"LGA1700","tdp_watts":125, "cores":20, "threads":28, "base_ghz":3.4,"boost_ghz":5.6,"has_igpu":true}'),

-- out of stock: tests in-stock filter on high-end Intel
('cpu-005', 'cpu', 'Core i9-14900K',      'Intel', 52000, FALSE,
 '{"socket":"LGA1700","tdp_watts":125, "cores":24, "threads":32, "base_ghz":3.2,"boost_ghz":6.0,"has_igpu":true}'),

('cpu-006', 'cpu', 'Core Ultra 5 245K',   'Intel', 27000, TRUE,
 '{"socket":"LGA1851","tdp_watts":125, "cores":14, "threads":14, "base_ghz":3.6,"boost_ghz":5.2,"has_igpu":true}'),

('cpu-007', 'cpu', 'Core Ultra 9 285K',   'Intel', 49000, TRUE,
 '{"socket":"LGA1851","tdp_watts":125, "cores":24, "threads":24, "base_ghz":3.2,"boost_ghz":5.7,"has_igpu":true}'),

('cpu-008', 'cpu', 'Ryzen 5 5600X',       'AMD',   12500, TRUE,
 '{"socket":"AM4",    "tdp_watts":65,  "cores":6,  "threads":12, "base_ghz":3.7,"boost_ghz":4.6,"has_igpu":false}'),

('cpu-009', 'cpu', 'Ryzen 5 7600X',       'AMD',   19500, TRUE,
 '{"socket":"AM5",    "tdp_watts":105, "cores":6,  "threads":12, "base_ghz":4.7,"boost_ghz":5.3,"has_igpu":true}'),

('cpu-010', 'cpu', 'Ryzen 7 7700X',       'AMD',   28000, TRUE,
 '{"socket":"AM5",    "tdp_watts":105, "cores":8,  "threads":16, "base_ghz":4.5,"boost_ghz":5.4,"has_igpu":true}'),

('cpu-011', 'cpu', 'Ryzen 9 7900X',       'AMD',   42000, TRUE,
 '{"socket":"AM5",    "tdp_watts":170, "cores":12, "threads":24, "base_ghz":4.7,"boost_ghz":5.6,"has_igpu":true}'),

('cpu-012', 'cpu', 'Ryzen 9 7950X',       'AMD',   62000, TRUE,
 '{"socket":"AM5",    "tdp_watts":170, "cores":16, "threads":32, "base_ghz":4.5,"boost_ghz":5.7,"has_igpu":true}');


-- ─────────────────────────────────────────────────────────────────────────────
-- RAM  specs: capacity_gb, speed_mhz, ddr_gen, kit_count
-- ─────────────────────────────────────────────────────────────────────────────
INSERT INTO catalog (product_id, category, name, brand, price_inr, in_stock, specs) VALUES

('ram-001', 'ram', 'Ripjaws V DDR4-3200 16GB Kit',            'G.Skill',  3800, TRUE,
 '{"capacity_gb":16, "speed_mhz":3200, "ddr_gen":4, "kit_count":2}'),

('ram-002', 'ram', 'FURY Beast DDR4-3600 16GB Kit',           'Kingston', 4200, TRUE,
 '{"capacity_gb":16, "speed_mhz":3600, "ddr_gen":4, "kit_count":2}'),

('ram-003', 'ram', 'Vengeance LPX DDR4-3200 32GB Kit',        'Corsair',  7200, TRUE,
 '{"capacity_gb":32, "speed_mhz":3200, "ddr_gen":4, "kit_count":2}'),

('ram-004', 'ram', 'Ripjaws V DDR4-3600 32GB Kit',            'G.Skill',  8000, TRUE,
 '{"capacity_gb":32, "speed_mhz":3600, "ddr_gen":4, "kit_count":2}'),

-- out of stock: tests DDR4 32 GB filter
('ram-005', 'ram', 'Pro DDR4-3200 32GB Kit',                  'Crucial',  6500, FALSE,
 '{"capacity_gb":32, "speed_mhz":3200, "ddr_gen":4, "kit_count":2}'),

('ram-006', 'ram', 'FURY Beast DDR5-5200 16GB Kit',           'Kingston', 6000, TRUE,
 '{"capacity_gb":16, "speed_mhz":5200, "ddr_gen":5, "kit_count":2}'),

('ram-007', 'ram', 'Trident Z5 RGB DDR5-6000 32GB Kit',       'G.Skill', 12000, TRUE,
 '{"capacity_gb":32, "speed_mhz":6000, "ddr_gen":5, "kit_count":2}'),

('ram-008', 'ram', 'Vengeance RGB DDR5-5600 32GB Kit',        'Corsair', 13500, TRUE,
 '{"capacity_gb":32, "speed_mhz":5600, "ddr_gen":5, "kit_count":2}'),

('ram-009', 'ram', 'FURY Beast DDR5-6000 32GB Kit',           'Kingston',10500, TRUE,
 '{"capacity_gb":32, "speed_mhz":6000, "ddr_gen":5, "kit_count":2}'),

('ram-010', 'ram', 'Flare X5 DDR5-6000 32GB Kit',            'G.Skill', 11000, TRUE,
 '{"capacity_gb":32, "speed_mhz":6000, "ddr_gen":5, "kit_count":2}'),

('ram-011', 'ram', 'Dominator Platinum RGB DDR5-6200 64GB Kit','Corsair', 22000, TRUE,
 '{"capacity_gb":64, "speed_mhz":6200, "ddr_gen":5, "kit_count":2}');


-- ─────────────────────────────────────────────────────────────────────────────
-- Storage  specs: capacity_gb, interface, read_mbps, write_mbps
-- ─────────────────────────────────────────────────────────────────────────────
INSERT INTO catalog (product_id, category, name, brand, price_inr, in_stock, specs) VALUES

('storage-001', 'storage', 'NV2 1TB M.2 NVMe',             'Kingston', 4500, TRUE,
 '{"capacity_gb":1000, "interface":"M.2 NVMe Gen4", "read_mbps":3500,  "write_mbps":2100}'),

('storage-002', 'storage', 'Blue SN580 1TB M.2 NVMe',      'WD',       5500, TRUE,
 '{"capacity_gb":1000, "interface":"M.2 NVMe Gen4", "read_mbps":4150,  "write_mbps":4150}'),

('storage-003', 'storage', '980 Pro 1TB M.2 NVMe',         'Samsung',  7000, TRUE,
 '{"capacity_gb":1000, "interface":"M.2 NVMe Gen4", "read_mbps":7000,  "write_mbps":5100}'),

('storage-004', 'storage', 'Black SN850X 1TB M.2 NVMe',    'WD',       7800, TRUE,
 '{"capacity_gb":1000, "interface":"M.2 NVMe Gen4", "read_mbps":7300,  "write_mbps":6600}'),

('storage-005', 'storage', '990 Pro 2TB M.2 NVMe',         'Samsung', 13500, TRUE,
 '{"capacity_gb":2000, "interface":"M.2 NVMe Gen4", "read_mbps":7450,  "write_mbps":6900}'),

-- out of stock: tests Gen4 2 TB filter
('storage-006', 'storage', 'FireCuda 530 2TB M.2 NVMe',    'Seagate', 14500, FALSE,
 '{"capacity_gb":2000, "interface":"M.2 NVMe Gen4", "read_mbps":7300,  "write_mbps":6900}'),

('storage-007', 'storage', 'Black SN850X 2TB M.2 NVMe',    'WD',      14000, TRUE,
 '{"capacity_gb":2000, "interface":"M.2 NVMe Gen4", "read_mbps":7300,  "write_mbps":6600}'),

('storage-008', 'storage', 'T705 2TB M.2 NVMe Gen5',       'Crucial', 18000, TRUE,
 '{"capacity_gb":2000, "interface":"M.2 NVMe Gen5", "read_mbps":14100, "write_mbps":12600}'),

('storage-009', 'storage', '870 EVO 1TB SATA SSD',         'Samsung',  6500, TRUE,
 '{"capacity_gb":1000, "interface":"SATA III",       "read_mbps":560,   "write_mbps":530}'),

('storage-010', 'storage', 'MX500 2TB SATA SSD',           'Crucial',  8500, TRUE,
 '{"capacity_gb":2000, "interface":"SATA III",       "read_mbps":560,   "write_mbps":510}'),

('storage-011', 'storage', 'Barracuda 2TB HDD 7200RPM',    'Seagate',  4000, TRUE,
 '{"capacity_gb":2000, "interface":"SATA III",       "read_mbps":190,   "write_mbps":190}'),

('storage-012', 'storage', 'Red Plus 4TB NAS HDD',         'WD',       8000, TRUE,
 '{"capacity_gb":4000, "interface":"SATA III",       "read_mbps":180,   "write_mbps":180}');


-- ─────────────────────────────────────────────────────────────────────────────
-- Motherboard  specs: socket, chipset, form_factor, max_ram_gb, ram_slots,
--                     pcie_slots, ddr_type (added: critical for RAM compat)
-- ─────────────────────────────────────────────────────────────────────────────
INSERT INTO catalog (product_id, category, name, brand, price_inr, in_stock, specs) VALUES

('mb-001', 'motherboard', 'PRO H610M-E DDR4',               'MSI',     7000, TRUE,
 '{"socket":"LGA1700","chipset":"H610", "form_factor":"mATX","max_ram_gb":64,  "ram_slots":2,"pcie_slots":1,"ddr_type":4}'),

('mb-002', 'motherboard', 'B760M DS3H DDR4',                'Gigabyte', 9000, TRUE,
 '{"socket":"LGA1700","chipset":"B760", "form_factor":"mATX","max_ram_gb":128, "ram_slots":2,"pcie_slots":1,"ddr_type":4}'),

('mb-003', 'motherboard', 'Prime B760M-A DDR4',             'ASUS',   10000, TRUE,
 '{"socket":"LGA1700","chipset":"B760", "form_factor":"mATX","max_ram_gb":128, "ram_slots":4,"pcie_slots":1,"ddr_type":4}'),

('mb-004', 'motherboard', 'TUF Gaming B760-Plus WiFi DDR5', 'ASUS',   17500, TRUE,
 '{"socket":"LGA1700","chipset":"B760", "form_factor":"ATX", "max_ram_gb":192, "ram_slots":4,"pcie_slots":2,"ddr_type":5}'),

('mb-005', 'motherboard', 'MAG B760M Mortar WiFi DDR5',     'MSI',    15000, TRUE,
 '{"socket":"LGA1700","chipset":"B760", "form_factor":"mATX","max_ram_gb":192, "ram_slots":4,"pcie_slots":1,"ddr_type":5}'),

-- out of stock: tests high-end LGA1700 Z790 filter
('mb-006', 'motherboard', 'ROG Strix Z790-E Gaming WiFi II','ASUS',   40000, FALSE,
 '{"socket":"LGA1700","chipset":"Z790", "form_factor":"ATX", "max_ram_gb":192, "ram_slots":4,"pcie_slots":3,"ddr_type":5}'),

('mb-007', 'motherboard', 'Z890 Aorus Elite WiFi7',         'Gigabyte',30000, TRUE,
 '{"socket":"LGA1851","chipset":"Z890", "form_factor":"ATX", "max_ram_gb":256, "ram_slots":4,"pcie_slots":2,"ddr_type":5}'),

('mb-008', 'motherboard', 'B650M DS3H',                     'Gigabyte',13500, TRUE,
 '{"socket":"AM5",    "chipset":"B650", "form_factor":"mATX","max_ram_gb":192, "ram_slots":4,"pcie_slots":1,"ddr_type":5}'),

('mb-009', 'motherboard', 'PRO B650M-A WiFi',               'MSI',    16000, TRUE,
 '{"socket":"AM5",    "chipset":"B650", "form_factor":"mATX","max_ram_gb":192, "ram_slots":4,"pcie_slots":1,"ddr_type":5}'),

('mb-010', 'motherboard', 'TUF Gaming B650-Plus WiFi',      'ASUS',   19500, TRUE,
 '{"socket":"AM5",    "chipset":"B650", "form_factor":"ATX", "max_ram_gb":192, "ram_slots":4,"pcie_slots":2,"ddr_type":5}'),

('mb-011', 'motherboard', 'MAG X670E Tomahawk WiFi',        'MSI',    25500, TRUE,
 '{"socket":"AM5",    "chipset":"X670E","form_factor":"ATX", "max_ram_gb":256, "ram_slots":4,"pcie_slots":3,"ddr_type":5}'),

('mb-012', 'motherboard', 'B450M DS3H',                     'Gigabyte', 6000, TRUE,
 '{"socket":"AM4",    "chipset":"B450", "form_factor":"mATX","max_ram_gb":128, "ram_slots":4,"pcie_slots":1,"ddr_type":4}');


-- ─────────────────────────────────────────────────────────────────────────────
-- PSU  specs: wattage, efficiency_rating, modular
-- ─────────────────────────────────────────────────────────────────────────────
INSERT INTO catalog (product_id, category, name, brand, price_inr, in_stock, specs) VALUES

('psu-001', 'psu', 'NE 550W 80+ Bronze',             'Antec',        4000, TRUE,
 '{"wattage":550,  "efficiency_rating":"80+ Bronze",   "modular":"non"}'),

('psu-002', 'psu', 'MWE 650W 80+ Bronze',            'Cooler Master',4800, TRUE,
 '{"wattage":650,  "efficiency_rating":"80+ Bronze",   "modular":"non"}'),

('psu-003', 'psu', 'CV650 650W 80+ Bronze',          'Corsair',      5200, TRUE,
 '{"wattage":650,  "efficiency_rating":"80+ Bronze",   "modular":"non"}'),

('psu-004', 'psu', 'Focus GX-650 80+ Gold',          'Seasonic',     8000, TRUE,
 '{"wattage":650,  "efficiency_rating":"80+ Gold",     "modular":"full"}'),

-- out of stock: tests mid-range Gold semi-modular filter
('psu-005', 'psu', 'RM750e 80+ Gold',                'Corsair',      9000, FALSE,
 '{"wattage":750,  "efficiency_rating":"80+ Gold",     "modular":"semi"}'),

('psu-006', 'psu', 'V750 Gold V2 80+ Gold',          'Cooler Master',10500, TRUE,
 '{"wattage":750,  "efficiency_rating":"80+ Gold",     "modular":"full"}'),

('psu-007', 'psu', 'Focus GX-850 80+ Gold',          'Seasonic',    11000, TRUE,
 '{"wattage":850,  "efficiency_rating":"80+ Gold",     "modular":"full"}'),

('psu-008', 'psu', 'HCG 850W 80+ Gold',              'Antec',       10000, TRUE,
 '{"wattage":850,  "efficiency_rating":"80+ Gold",     "modular":"full"}'),

('psu-009', 'psu', 'Straight Power 11 750W 80+ Plat','be quiet!',   13000, TRUE,
 '{"wattage":750,  "efficiency_rating":"80+ Platinum", "modular":"full"}'),

('psu-010', 'psu', 'RM1000e 80+ Gold',               'Corsair',     14000, TRUE,
 '{"wattage":1000, "efficiency_rating":"80+ Gold",     "modular":"semi"}'),

('psu-011', 'psu', 'Prime TX-1000 80+ Titanium',     'Seasonic',    22000, TRUE,
 '{"wattage":1000, "efficiency_rating":"80+ Titanium", "modular":"full"}');


-- ─────────────────────────────────────────────────────────────────────────────
-- Case  specs: form_factor_support, max_gpu_length_mm, max_cooler_height_mm
-- ─────────────────────────────────────────────────────────────────────────────
INSERT INTO catalog (product_id, category, name, brand, price_inr, in_stock, specs) VALUES

('case-001', 'case', 'CC360 ARGB mATX',         'DeepCool',      4500, TRUE,
 '{"form_factor_support":["mATX","ITX"],          "max_gpu_length_mm":320,"max_cooler_height_mm":165}'),

('case-002', 'case', 'MasterBox MB311L mATX',   'Cooler Master', 4800, TRUE,
 '{"form_factor_support":["mATX","ITX"],          "max_gpu_length_mm":360,"max_cooler_height_mm":155}'),

-- out of stock: tests mid-range ATX filter
('case-003', 'case', 'DF700 FLUX ATX',          'Antec',         6500, FALSE,
 '{"form_factor_support":["ATX","mATX","ITX"],    "max_gpu_length_mm":380,"max_cooler_height_mm":165}'),

('case-004', 'case', 'Eclipse P360A ATX',       'Phanteks',      7200, TRUE,
 '{"form_factor_support":["ATX","mATX","ITX"],    "max_gpu_length_mm":435,"max_cooler_height_mm":160}'),

('case-005', 'case', 'HAF 500 ATX',             'Cooler Master', 8000, TRUE,
 '{"form_factor_support":["ATX","mATX","ITX"],    "max_gpu_length_mm":410,"max_cooler_height_mm":166}'),

('case-006', 'case', 'LANCOOL 216 ATX',         'Lian Li',       8500, TRUE,
 '{"form_factor_support":["ATX","mATX","ITX"],    "max_gpu_length_mm":435,"max_cooler_height_mm":169}'),

('case-007', 'case', 'Pure Base 500DX ATX',     'be quiet!',     9500, TRUE,
 '{"form_factor_support":["ATX","mATX","ITX"],    "max_gpu_length_mm":369,"max_cooler_height_mm":190}'),

('case-008', 'case', 'H7 Flow ATX',             'NZXT',         10500, TRUE,
 '{"form_factor_support":["ATX","mATX","ITX"],    "max_gpu_length_mm":400,"max_cooler_height_mm":185}'),

('case-009', 'case', 'Define 7 ATX',            'Fractal Design',13500, TRUE,
 '{"form_factor_support":["ATX","mATX","ITX"],    "max_gpu_length_mm":491,"max_cooler_height_mm":185}'),

('case-010', 'case', 'O11 Dynamic EVO ATX',     'Lian Li',      16000, TRUE,
 '{"form_factor_support":["ATX","mATX","ITX"],    "max_gpu_length_mm":420,"max_cooler_height_mm":167}'),

('case-011', 'case', 'H1 V2 Mini-ITX',          'NZXT',         14500, TRUE,
 '{"form_factor_support":["ITX"],                 "max_gpu_length_mm":324,"max_cooler_height_mm":0}');


-- ─────────────────────────────────────────────────────────────────────────────
-- Cooler  specs: type, tdp_support_watts, height_mm, radiator_size_mm
--               socket_compat added — required to verify cooler fits CPU socket
-- ─────────────────────────────────────────────────────────────────────────────
INSERT INTO catalog (product_id, category, name, brand, price_inr, in_stock, specs) VALUES

('cooler-001', 'cooler', 'AK400 Air CPU Cooler',             'DeepCool',     2500, TRUE,
 '{"type":"air","tdp_support_watts":220,"height_mm":155,"radiator_size_mm":null,
   "socket_compat":["LGA1700","LGA1851","AM4","AM5"]}'),

('cooler-002', 'cooler', 'Hyper 212 Halo Black',             'Cooler Master',2800, TRUE,
 '{"type":"air","tdp_support_watts":180,"height_mm":158,"radiator_size_mm":null,
   "socket_compat":["LGA1700","LGA1851","AM4","AM5"]}'),

('cooler-003', 'cooler', 'SE-207-XT Advanced Air Cooler',    'ID-Cooling',   3800, TRUE,
 '{"type":"air","tdp_support_watts":260,"height_mm":161,"radiator_size_mm":null,
   "socket_compat":["LGA1700","LGA1851","AM4","AM5"]}'),

('cooler-004', 'cooler', 'NH-U12A Air Cooler',               'Noctua',       6500, TRUE,
 '{"type":"air","tdp_support_watts":250,"height_mm":158,"radiator_size_mm":null,
   "socket_compat":["LGA1700","LGA1851","AM4","AM5"]}'),

-- out of stock: tests premium air cooler filter
('cooler-005', 'cooler', 'NH-D15 Air Cooler',                'Noctua',       8500, FALSE,
 '{"type":"air","tdp_support_watts":280,"height_mm":165,"radiator_size_mm":null,
   "socket_compat":["LGA1700","LGA1851","AM4","AM5"]}'),

('cooler-006', 'cooler', 'LS520 SE AIO 240mm',               'DeepCool',     6800, TRUE,
 '{"type":"aio","tdp_support_watts":250,"height_mm":null,"radiator_size_mm":240,
   "socket_compat":["LGA1700","LGA1851","AM4","AM5"]}'),

('cooler-007', 'cooler', 'MasterLiquid 360L Core ARGB AIO',  'Cooler Master',9500, TRUE,
 '{"type":"aio","tdp_support_watts":300,"height_mm":null,"radiator_size_mm":360,
   "socket_compat":["LGA1700","LGA1851","AM4","AM5"]}'),

('cooler-008', 'cooler', 'iCUE H100i RGB Elite AIO 240mm',   'Corsair',     11000, TRUE,
 '{"type":"aio","tdp_support_watts":280,"height_mm":null,"radiator_size_mm":240,
   "socket_compat":["LGA1700","LGA1851","AM4","AM5"]}'),

('cooler-009', 'cooler', 'Kraken 280 AIO',                   'NZXT',        12500, TRUE,
 '{"type":"aio","tdp_support_watts":300,"height_mm":null,"radiator_size_mm":280,
   "socket_compat":["LGA1700","LGA1851","AM4","AM5"]}'),

('cooler-010', 'cooler', 'Galahad AIO 360 SL V2',            'Lian Li',     14000, TRUE,
 '{"type":"aio","tdp_support_watts":350,"height_mm":null,"radiator_size_mm":360,
   "socket_compat":["LGA1700","LGA1851","AM4","AM5"]}');


-- ─────────────────────────────────────────────────────────────────────────────
-- Fans  specs: size_mm, static_pressure, airflow_cfm
-- ─────────────────────────────────────────────────────────────────────────────
INSERT INTO catalog (product_id, category, name, brand, price_inr, in_stock, specs) VALUES

('fans-001', 'fans', 'P12 PWM PST 120mm',                 'Arctic',    700, TRUE,
 '{"size_mm":120,"static_pressure":2.20,"airflow_cfm":48.8}'),

('fans-002', 'fans', 'F12 PWM PST 5-Pack 120mm',          'Arctic',   2200, TRUE,
 '{"size_mm":120,"static_pressure":1.85,"airflow_cfm":52.5}'),

-- out of stock: tests single 120 mm performance fan filter
('fans-003', 'fans', 'NF-P12 redux-1700 PWM 120mm',       'Noctua',   2200, FALSE,
 '{"size_mm":120,"static_pressure":2.83,"airflow_cfm":70.0}'),

('fans-004', 'fans', 'Silent Wings 4 120mm PWM',           'be quiet!',2800, TRUE,
 '{"size_mm":120,"static_pressure":2.59,"airflow_cfm":50.5}'),

('fans-005', 'fans', 'LL120 RGB 3-Pack 120mm',             'Corsair',  5200, TRUE,
 '{"size_mm":120,"static_pressure":1.61,"airflow_cfm":43.3}'),

('fans-006', 'fans', 'UNI Fan SL120 V2 ARGB 3-Pack 120mm','Lian Li',  6500, TRUE,
 '{"size_mm":120,"static_pressure":2.45,"airflow_cfm":51.5}'),

('fans-007', 'fans', 'FC120 ARGB 3-Pack 120mm',            'DeepCool', 2400, TRUE,
 '{"size_mm":120,"static_pressure":1.51,"airflow_cfm":56.0}'),

('fans-008', 'fans', 'NF-A14 PWM 140mm',                   'Noctua',  3500, TRUE,
 '{"size_mm":140,"static_pressure":2.37,"airflow_cfm":82.5}'),

('fans-009', 'fans', 'Silent Wings 4 140mm PWM High-Speed', 'be quiet!',3200,TRUE,
 '{"size_mm":140,"static_pressure":2.59,"airflow_cfm":74.4}'),

('fans-010', 'fans', 'P14 PWM PST 140mm',                  'Arctic',   800, TRUE,
 '{"size_mm":140,"static_pressure":2.50,"airflow_cfm":68.1}');

COMMIT;
