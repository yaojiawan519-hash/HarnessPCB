# 块库索引

共 **108 块**，全部 `license_tier: permissive`（可公开发行）。每块 = 一块人画好的电路，含原理图、板（2 层、不带铺铜）、块卡（端口/轨/内部网/放置先验）与测量产物（`_geom/`）。

**口岸按焊盘记**：拼板时从焊盘重新拉线，不指望块里切剩的出口铜能对接上。详细口径见 `docs/块库说明.md`。

## actuator（2）

| 块 | 件 | 网 | 铜 | 来源板 | 许可证 |
|---|---|---|---|---|---|
| [`ir_tx_cc`](ir_tx_cc/) | 15 | 12 | 69 段 + 4 孔 | Bus Pirate 5 IR Toy 适配板（3REV3） | MIT |
| [`vib_motor_drv`](vib_motor_drv/) | 5 | 5 | 28 段 + 3 孔 | Watchy | MIT |

## analog（6）

| 块 | 件 | 网 | 铜 | 来源板 | 许可证 |
|---|---|---|---|---|---|
| [`bp5_analog_buffers`](bp5_analog_buffers/) | 12 | 26 | 266 段 + 27 孔 | Bus Pirate 5 主板 REV10A（`bus_pirate_pcb/5-REV10A`） | MIT |
| [`bp5_analog_mux`](bp5_analog_mux/) | 17 | 38 | 198 段 + 18 孔 | Bus Pirate 5 主板 REV10A（`bus_pirate_pcb/5-REV10A`） | MIT |
| [`bp5_current_limit`](bp5_current_limit/) | 12 | 11 | 84 段 + 8 孔 | Bus Pirate 5 主板 REV10A（`bus_pirate_pcb/5-REV10A`） | MIT |
| [`bp5_current_sense`](bp5_current_sense/) | 8 | 6 | 22 段 + 1 孔 | Bus Pirate 5 主板 REV10A（`bus_pirate_pcb/5-REV10A`） | MIT |
| [`bp5_vreg_adj_buffer`](bp5_vreg_adj_buffer/) | 5 | 5 | 10 段 + 1 孔 | Bus Pirate 5 主板 REV10A（`bus_pirate_pcb/5-REV10A`） | MIT |
| [`nrf24_match_network`](nrf24_match_network/) | 8 | 6 | 22 段 + 2 孔 | STRF (2.4GHz RF-to-USB dongle) | MIT |

## clock（1）

| 块 | 件 | 网 | 铜 | 来源板 | 许可证 |
|---|---|---|---|---|---|
| [`xtal_nrf24`](xtal_nrf24/) | 4 | 3 | 19 段 + 0 孔 | STRF (2.4GHz RF-to-USB dongle) | MIT |

## connector（29）

| 块 | 件 | 网 | 铜 | 来源板 | 许可证 |
|---|---|---|---|---|---|
| [`bp5_bpio_headers`](bp5_bpio_headers/) | 3 | 10 | 57 段 + 2 孔 | Bus Pirate 5 主板 REV10A（`bus_pirate_pcb/5-REV10A`） | MIT |
| [`bp5_usb_c_port`](bp5_usb_c_port/) | 8 | 11 | 26 段 + 2 孔 | Bus Pirate 5 主板 REV10A（`bus_pirate_pcb/5-REV10A`） | MIT |
| [`flash_lotes_port`](flash_lotes_port/) | 4 | 12 | 43 段 + 2 孔 | Bus Pirate 5 flash adapter (SOP/LOTES) REV3 | MIT |
| [`flash_lotes_sockets`](flash_lotes_sockets/) | 4 | 8 | 69 段 + 13 孔 | Bus Pirate 5 flash adapter (SOP/LOTES) REV3 | MIT |
| [`flash_sop_port`](flash_sop_port/) | 4 | 12 | 48 段 + 3 孔 | Bus Pirate 5 flash adapter (SOP) REV3 | MIT |
| [`flash_sop_socket16`](flash_sop_socket16/) | 4 | 10 | 27 段 + 0 孔 | Bus Pirate 5 flash adapter (SOP) REV3 | MIT |
| [`flash_sop_socket20`](flash_sop_socket20/) | 2 | 11 | 38 段 + 2 孔 | Bus Pirate 5 flash adapter (SOP) REV3 | MIT |
| [`flash_wson_port`](flash_wson_port/) | 4 | 12 | 47 段 + 4 孔 | Bus Pirate 5 flash adapter (WSON) REV3 | MIT |
| [`flash_wson_sockets`](flash_wson_sockets/) | 4 | 8 | 55 段 + 6 孔 | Bus Pirate 5 flash adapter (WSON) REV3 | MIT |
| [`flash_zif_port`](flash_zif_port/) | 4 | 12 | 45 段 + 3 孔 | Bus Pirate 5 flash adapter (DIP/ZIF) REV3 | MIT |
| [`flash_zif_socket`](flash_zif_socket/) | 4 | 10 | 59 段 + 0 孔 | Bus Pirate 5 flash adapter (DIP/ZIF) REV3 | MIT |
| [`grove_gravity_ports`](grove_gravity_ports/) | 2 | 4 | 12 段 + 2 孔 | Bus Pirate 5 I2C Quick Connect Plank v1 REV1 | MIT |
| [`kb_host_header`](kb_host_header/) | 2 | 10 | 15 段 + 0 孔 | Bus Pirate 5 Kaybee Plank（PS/2 键盘/鼠标转接板）REV1 | MIT |
| [`kb_ps2_dual_port`](kb_ps2_dual_port/) | 5 | 10 | 23 段 + 2 孔 | Bus Pirate 5 Kaybee Plank（PS/2 键盘/鼠标转接板）REV1 | MIT |
| [`kb_usb_a_port`](kb_usb_a_port/) | 3 | 5 | 17 段 + 2 孔 | Bus Pirate 5 Kaybee Plank（PS/2 键盘/鼠标转接板）REV1 | MIT |
| [`kb_usb_c_port`](kb_usb_c_port/) | 5 | 9 | 39 段 + 4 孔 | Bus Pirate 5 Kaybee Plank（PS/2 键盘/鼠标转接板）REV1 | MIT |
| [`kb_vdd_header`](kb_vdd_header/) | 1 | 2 | 0 段 + 0 孔 | Bus Pirate 5 Kaybee Plank（PS/2 键盘/鼠标转接板）REV1 | MIT |
| [`ph_ports`](ph_ports/) | 2 | 4 | 4 段 + 0 孔 | Bus Pirate 5 I2C Quick Connect Plank v1 REV1 | MIT |
| [`plank_edge`](plank_edge/) | 1 | 5 | 14 段 + 0 孔 | Bus Pirate 5 I2C Quick Connect Plank v1 REV1 | MIT |
| [`plank_io_headers`](plank_io_headers/) | 2 | 10 | 10 段 + 0 孔 | Bus Pirate 5 I2C Quick Connect Plank v1 REV1 | MIT |
| [`proto_header_trio`](proto_header_trio/) | 3 | 10 | 17 段 + 0 孔 | KiCAD_Access/hw_sources/BusPirate5-hardware/adapter-blank-plank-1REV0B/blank-plank-rev0b.kicad_sch.kicad_pcb | MIT |
| [`proto_switched_headers`](proto_switched_headers/) | 4 | ? | — | KiCAD_Access/hw_sources/BusPirate5-hardware/adapter-blank-plank-1REV0B/blank-plank-rev0b.kicad_sch | MIT |
| [`rs232_headers`](rs232_headers/) | 2 | 10 | 10 段 + 0 孔 | Bus Pirate 5 RS232 双通道转接板 REV0 | MIT |
| [`sim_headers`](sim_headers/) | 2 | 10 | 11 段 + 0 孔 | Bus Pirate 5 SIM 卡适配板（adapter-sim-iccard-1REV3C） | MIT |
| [`sim_socket_te5145`](sim_socket_te5145/) | 1 | 10 | 186 段 + 25 孔 | Bus Pirate 5 SIM 卡适配板（adapter-sim-iccard-1REV3C） | MIT |
| [`sim_sockets`](sim_sockets/) | 3 | 9 | 120 段 + 16 孔 | Bus Pirate 5 SIM 卡适配板（adapter-sim-iccard-1REV3C） | MIT |
| [`stemma_qwiic_ports`](stemma_qwiic_ports/) | 4 | 4 | 15 段 + 4 孔 | Bus Pirate 5 I2C Quick Connect Plank v1 REV1 | MIT |
| [`wago_kf141r_adapter`](wago_kf141r_adapter/) | 2 | 10 | 30 段 + 0 孔 | wago kf141r adapter.kicad_sch / .kicad_pcb | MIT |
| [`wago_kf141v_adapter`](wago_kf141v_adapter/) | 2 | 10 | 30 段 + 0 孔 | wago kf141v adapter.kicad_sch / .kicad_pcb | MIT |

## debug（2）

| 块 | 件 | 网 | 铜 | 来源板 | 许可证 |
|---|---|---|---|---|---|
| [`bp5_swd_header`](bp5_swd_header/) | 1 | 3 | 2 段 + 0 孔 | Bus Pirate 5 主板 REV10A（`bus_pirate_pcb/5-REV10A`） | MIT |
| [`swd_connector`](swd_connector/) | 2 | 8 | 3 段 + 0 孔 | STRF (2.4GHz RF-to-USB dongle) | MIT |

## display（1）

| 块 | 件 | 网 | 铜 | 来源板 | 许可证 |
|---|---|---|---|---|---|
| [`bp5_lcd_display`](bp5_lcd_display/) | 4 | 12 | 255 段 + 31 孔 | Bus Pirate 5 主板 REV10A（`bus_pirate_pcb/5-REV10A`） | MIT |

## indicator（13）

| 块 | 件 | 网 | 铜 | 来源板 | 许可证 |
|---|---|---|---|---|---|
| [`ddr5_status_leds`](ddr5_status_leds/) | 4 | 5 | 15 段 + 1 孔 | Bus Pirate 5 DDR5 内存适配板（adapter-ram-ddr5-1REV2） | MIT |
| [`flash_lotes_led`](flash_lotes_led/) | 2 | 3 | 1 段 + 0 孔 | Bus Pirate 5 flash adapter (SOP/LOTES) REV3 | MIT |
| [`flash_wson_led`](flash_wson_led/) | 2 | 3 | 1 段 + 0 孔 | Bus Pirate 5 flash adapter (WSON) REV3 | MIT |
| [`irtoy_power_led`](irtoy_power_led/) | 2 | 3 | 1 段 + 0 孔 | Bus Pirate 5 IR Toy 适配板（3REV3） | MIT |
| [`kb_power_led`](kb_power_led/) | 2 | 3 | 14 段 + 2 孔 | Bus Pirate 5 Kaybee Plank（PS/2 键盘/鼠标转接板）REV1 | MIT |
| [`led_indicator`](led_indicator/) | 5 | 7 | 136 段 + 5 孔 | UPduino v3.01 | MIT |
| [`plank_power_led`](plank_power_led/) | 2 | 3 | 1 段 + 0 孔 | Bus Pirate 5 I2C Quick Connect Plank v1 REV1 | MIT |
| [`power_led`](power_led/) | 2 | 3 | 1 段 + 0 孔 | Bus Pirate 5 VPP Maker 适配板（1REV6） | MIT |
| [`proto_led_jumper`](proto_led_jumper/) | 3 | ? | — | KiCAD_Access/hw_sources/BusPirate5-hardware/adapter-blank-plank-1REV0B/blank-plank-rev0b.kicad_sch | MIT |
| [`proto_vout_indicator`](proto_vout_indicator/) | 2 | ? | — | KiCAD_Access/hw_sources/BusPirate5-hardware/adapter-blank-plank-1REV0B/blank-plank-rev0b.kicad_sch | MIT |
| [`rs232_vout_led`](rs232_vout_led/) | 2 | 3 | 3 段 + 0 孔 | Bus Pirate 5 RS232 双通道转接板 REV0 | MIT |
| [`sim_leds`](sim_leds/) | 4 | 5 | 9 段 + 0 孔 | Bus Pirate 5 SIM 卡适配板（adapter-sim-iccard-1REV3C） | MIT |
| [`status_led`](status_led/) | 2 | 5 | 3 段 + 0 孔 | esp-rust-board | CERN-OHL-P-2.0 |

## input（3）

| 块 | 件 | 网 | 铜 | 来源板 | 许可证 |
|---|---|---|---|---|---|
| [`bp5_user_button`](bp5_user_button/) | 1 | 4 | 2 段 + 0 孔 | Bus Pirate 5 主板 REV10A（`bus_pirate_pcb/5-REV10A`） | MIT |
| [`buttons_pullup`](buttons_pullup/) | 8 | 6 | 255 段 + 30 孔 | Watchy | MIT |
| [`kb_trig_header`](kb_trig_header/) | 2 | 3 | 3 段 + 0 孔 | Bus Pirate 5 Kaybee Plank（PS/2 键盘/鼠标转接板）REV1 | MIT |

## interface（22）

| 块 | 件 | 网 | 铜 | 来源板 | 许可证 |
|---|---|---|---|---|---|
| [`adapter_headers`](adapter_headers/) | 3 | 12 | 70 段 + 2 孔 | Bus Pirate 5 VPP Maker 适配板（1REV6） | MIT |
| [`boot_straps`](boot_straps/) | 7 | 9 | 58 段 + 7 孔 | UPduino v3.01 | MIT |
| [`bp5_io_buffer_0_3`](bp5_io_buffer_0_3/) | 17 | 23 | 235 段 + 42 孔 | Bus Pirate 5 主板 REV10A（`bus_pirate_pcb/5-REV10A`） | MIT |
| [`bp5_io_buffer_4_7`](bp5_io_buffer_4_7/) | 17 | 23 | 227 段 + 39 孔 | Bus Pirate 5 主板 REV10A（`bus_pirate_pcb/5-REV10A`） | MIT |
| [`bp5_qspi_flash`](bp5_qspi_flash/) | 6 | 9 | 35 段 + 5 孔 | Bus Pirate 5 主板 REV10A（`bus_pirate_pcb/5-REV10A`） | MIT |
| [`bp5_spi_nand`](bp5_spi_nand/) | 1 | 6 | 14 段 + 2 孔 | Bus Pirate 5 主板 REV10A（`bus_pirate_pcb/5-REV10A`） | MIT |
| [`connectors_io`](connectors_io/) | 6 | 46 | 833 段 + 57 孔 | UPduino v3.01 | MIT |
| [`ddr5_bus_headers`](ddr5_bus_headers/) | 2 | 10 | 9 段 + 0 孔 | Bus Pirate 5 DDR5 内存适配板（adapter-ram-ddr5-1REV2） | MIT |
| [`ddr5_sodimm_socket`](ddr5_sodimm_socket/) | 1 | 97 | 3 段 + 1 孔 | Bus Pirate 5 DDR5 内存适配板（adapter-ram-ddr5-1REV2） | MIT |
| [`ddr5_udimm_socket`](ddr5_udimm_socket/) | 1 | 97 | 7 段 + 2 孔 | Bus Pirate 5 DDR5 内存适配板（adapter-ram-ddr5-1REV2） | MIT |
| [`ee_config`](ee_config/) | 6 | 6 | 122 段 + 5 孔 | UPduino v3.01 | MIT |
| [`epaper_driver`](epaper_driver/) | 19 | 24 | 129 段 + 9 孔 | Watchy | MIT |
| [`flash_qspi`](flash_qspi/) | 8 | 10 | 92 段 + 10 孔 | UPduino v3.01 | MIT |
| [`i2c_level_shift`](i2c_level_shift/) | 3 | 7 | 9 段 + 0 孔 | Bus Pirate 5 DDR5 内存适配板（adapter-ram-ddr5-1REV2） | MIT |
| [`irtoy_headers`](irtoy_headers/) | 2 | 10 | 22 段 + 0 孔 | Bus Pirate 5 IR Toy 适配板（3REV3） | MIT |
| [`nrf24_transceiver`](nrf24_transceiver/) | 7 | 15 | 43 段 + 0 孔 | STRF (2.4GHz RF-to-USB dongle) | MIT |
| [`power_good_sense`](power_good_sense/) | 6 | 8 | 18 段 + 0 孔 | Bus Pirate 5 DDR5 内存适配板（adapter-ram-ddr5-1REV2） | MIT |
| [`rs232_dce`](rs232_dce/) | 7 | 21 | 53 段 + 4 孔 | Bus Pirate 5 RS232 双通道转接板 REV0 | MIT |
| [`rs232_dte`](rs232_dte/) | 7 | 21 | 42 段 + 2 孔 | Bus Pirate 5 RS232 双通道转接板 REV0 | MIT |
| [`usb_dev_esd`](usb_dev_esd/) | 5 | 8 | 31 段 + 0 孔 | Watchy | MIT |
| [`usb_esd_usblc6`](usb_esd_usblc6/) | 2 | 7 | 26 段 + 2 孔 | STRF (2.4GHz RF-to-USB dongle) | MIT |
| [`usb_ftdi_bridge`](usb_ftdi_bridge/) | 31 | 38 | 571 段 + 29 孔 | UPduino v3.01 | MIT |

## io（3）

| 块 | 件 | 网 | 铜 | 来源板 | 许可证 |
|---|---|---|---|---|---|
| [`bp5_io_expander`](bp5_io_expander/) | 9 | 39 | 207 段 + 15 孔 | Bus Pirate 5 主板 REV10A（`bus_pirate_pcb/5-REV10A`） | MIT |
| [`bp5_rgb_leds`](bp5_rgb_leds/) | 37 | 22 | 370 段 + 41 孔 | Bus Pirate 5 主板 REV10A（`bus_pirate_pcb/5-REV10A`） | MIT |
| [`bp5_switchable_pullups`](bp5_switchable_pullups/) | 11 | 19 | 236 段 + 22 孔 | Bus Pirate 5 主板 REV10A（`bus_pirate_pcb/5-REV10A`） | MIT |

## mcu（6）

| 块 | 件 | 网 | 铜 | 来源板 | 许可证 |
|---|---|---|---|---|---|
| [`bp5_mcu_rp2040`](bp5_mcu_rp2040/) | 18 | 46 | 274 段 + 41 孔 | Bus Pirate 5 主板 REV10A（`bus_pirate_pcb/5-REV10A`） | MIT |
| [`clock_12m`](clock_12m/) | 6 | 7 | 196 段 + 7 孔 | UPduino v3.01 | MIT |
| [`fpga_ice40up5k`](fpga_ice40up5k/) | 20 | 50 | 242 段 + 29 孔 | UPduino v3.01 | MIT |
| [`mcu_esp32c3_mini1`](mcu_esp32c3_mini1/) | 13 | 33 | 100 段 + 14 孔 | esp-rust-board | CERN-OHL-P-2.0 |
| [`mcu_esp32s3`](mcu_esp32s3/) | 23 | 54 | 311 段 + 34 孔 | Watchy | MIT |
| [`mcu_stm32l432`](mcu_stm32l432/) | 10 | 31 | 113 段 + 2 孔 | STRF (2.4GHz RF-to-USB dongle) | MIT |

## power（15）

| 块 | 件 | 网 | 铜 | 来源板 | 许可证 |
|---|---|---|---|---|---|
| [`bp5_backflow_prevent`](bp5_backflow_prevent/) | 7 | 7 | 40 段 + 4 孔 | Bus Pirate 5 主板 REV10A（`bus_pirate_pcb/5-REV10A`） | MIT |
| [`bp5_current_en_gate`](bp5_current_en_gate/) | 5 | 7 | 37 段 + 7 孔 | Bus Pirate 5 主板 REV10A（`bus_pirate_pcb/5-REV10A`） | MIT |
| [`bp5_ldo_3v3`](bp5_ldo_3v3/) | 3 | 3 | 2 段 + 0 孔 | Bus Pirate 5 主板 REV10A（`bus_pirate_pcb/5-REV10A`） | MIT |
| [`bp5_vreg_adj_ldo`](bp5_vreg_adj_ldo/) | 8 | 6 | 20 段 + 0 孔 | Bus Pirate 5 主板 REV10A（`bus_pirate_pcb/5-REV10A`） | MIT |
| [`ddr5_ldo_3v3`](ddr5_ldo_3v3/) | 4 | 3 | 5 段 + 0 孔 | Bus Pirate 5 DDR5 内存适配板（adapter-ram-ddr5-1REV2） | MIT |
| [`kb_backflow_prevent`](kb_backflow_prevent/) | 6 | 6 | 28 段 + 2 孔 | Bus Pirate 5 Kaybee Plank（PS/2 键盘/鼠标转接板）REV1 | MIT |
| [`kb_vout_switch`](kb_vout_switch/) | 1 | 3 | 5 段 + 2 孔 | Bus Pirate 5 Kaybee Plank（PS/2 键盘/鼠标转接板）REV1 | MIT |
| [`power_3v3_usb_ldo`](power_3v3_usb_ldo/) | 5 | 5 | 39 段 + 1 孔 | STRF (2.4GHz RF-to-USB dongle) | MIT |
| [`power_batt_ldo`](power_batt_ldo/) | 19 | 14 | 294 段 + 33 孔 | Watchy | MIT |
| [`regulators`](regulators/) | 6 | 6 | 53 段 + 2 孔 | UPduino v3.01 | MIT |
| [`sim_power_select`](sim_power_select/) | 2 | 6 | 21 段 + 4 孔 | Bus Pirate 5 SIM 卡适配板（adapter-sim-iccard-1REV3C） | MIT |
| [`usb_batt_switch`](usb_batt_switch/) | 3 | 4 | 12 段 + 0 孔 | esp-rust-board | CERN-OHL-P-2.0 |
| [`vpp_adc_divider`](vpp_adc_divider/) | 3 | 3 | 6 段 + 0 孔 | Bus Pirate 5 VPP Maker 适配板（1REV6） | MIT |
| [`vpp_boost`](vpp_boost/) | 7 | 6 | 32 段 + 1 孔 | Bus Pirate 5 VPP Maker 适配板（1REV6） | MIT |
| [`vpp_switch`](vpp_switch/) | 8 | 10 | 20 段 + 0 孔 | Bus Pirate 5 VPP Maker 适配板（1REV6） | MIT |

## sensor（5）

| 块 | 件 | 网 | 铜 | 来源板 | 许可证 |
|---|---|---|---|---|---|
| [`accel_bma423`](accel_bma423/) | 3 | 8 | 79 段 + 8 孔 | Watchy | MIT |
| [`ir_rcv_38k`](ir_rcv_38k/) | 4 | 4 | 12 段 + 0 孔 | Bus Pirate 5 IR Toy 适配板（3REV3） | MIT |
| [`ir_rcv_38k_barrier`](ir_rcv_38k_barrier/) | 4 | 4 | 17 段 + 2 孔 | Bus Pirate 5 IR Toy 适配板（3REV3） | MIT |
| [`ir_rcv_56k`](ir_rcv_56k/) | 4 | 4 | 12 段 + 0 孔 | Bus Pirate 5 IR Toy 适配板（3REV3） | MIT |
| [`ir_rcv_learner`](ir_rcv_learner/) | 4 | 4 | 8 段 + 0 孔 | Bus Pirate 5 IR Toy 适配板（3REV3） | MIT |
