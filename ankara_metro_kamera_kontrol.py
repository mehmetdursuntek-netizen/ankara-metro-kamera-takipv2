"""Ankara Metro Kamera Kontrol.

Bu yazılım; Ankara Metrosu bünyesindeki Pelco IP kameraların durumunu
izole (çevrimdışı/offline) yerel ağda (LAN) eşzamanlı olarak denetleyen,
donan veya arızalanan kameraları anında tespit eden gelişmiş bir izleme sistemidir.

Önemli Özellikler:
- %100 Çevrimdışı (Offline): İnternet bağlantısına hiçbir şekilde ihtiyaç duymaz.
- Kamera İsimlendirme & Konum Tanımlama: Her kameraya açıklayıcı ad ve konum atama (Turnike Giriş, Peron 1, Gişe vb.).
- Excel Rapor Dışa Aktarma: Sıfır harici paket bağımlılığıyla arızalı kameraları doğrudan Excel (.xls / .csv) olarak raporlama.
- Kamera Grubu ve IP Bloğu Yönetimi: İstasyon/bölge bazlı gruplar oluşturun (Milli Kütüphane, Ümitköy...).
  Örnek: 172.16.45.x bloğundaki kameralar otomatik "Milli Kütüphane", 172.16.47.x bloğundakiler "Ümitköy" grubuna atanır.
- Seçili Gruba / İstasyona Göre Tarama: Sadece seçilen istasyonu veya tüm istasyonları tarayabilme.
- Toplu IP İçe Aktarma (Bulk Import): TXT/CSV veya metin yapıştırarak IP ve kamera adlarını otomatik içe aktarma.
- Temiz Arıza Loglama: Gürültüsüz, sadece gerçek arıza ve kurtarma durumlarını kaydeden sistem.
- Anlık Arızalı Kamera Özeti: Her an güncel kalan logs/arizali_kameralar_guncel.txt dosyası.
- Tek Tıkla Çalıştırma (One-Click Run): BASLAT.bat ile doğrudan çift tıklanarak çalıştırılır.
"""

from __future__ import annotations

import argparse
import copy
import csv
from dataclasses import dataclass
from datetime import datetime
import json
import os
import platform
import re
import subprocess
import sys
import threading
import time
from typing import Dict, List, Optional, Tuple

try:
    import cv2
except ImportError:
    cv2 = None

try:
    from PyQt5 import QtCore, QtGui, QtWidgets
    from PyQt5.QtCore import QThread, Qt, pyqtSignal, pyqtSlot
    from PyQt5.QtGui import QColor, QImage, QPixmap
    from PyQt5.QtWidgets import (
        QApplication, QComboBox, QDialog, QFileDialog, QFrame,
        QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit,
        QListWidget, QListWidgetItem, QMainWindow, QMessageBox,
        QPushButton, QRadioButton, QSpinBox, QSplitter,
        QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout, QWidget,
    )
    PYQT5_AVAILABLE = True
except ImportError:
    PYQT5_AVAILABLE = False

os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|stimeout;3000000"

VARSAYILAN_KAMERA_IPLERI: List[str] = [
    "172.16.45.55",
    "172.16.45.56",
    "172.16.47.55",
    "172.16.47.56",
    "192.168.1.101",
    "192.168.1.102",
]

VARSAYILAN_BLOK_KURALLARI: Dict[str, str] = {
    "172.16.45.": "Milli Kütüphane",
    "172.16.47.": "Ümitköy",
}

VARSAYILAN_KAMERA_ISIMLERI: Dict[str, str] = {
    "172.16.45.55": "Turnike Giriş 1",
    "172.16.45.56": "Peron Doğu",
    "172.16.47.55": "Gişe Önü",
    "172.16.47.56": "Yürüyen Merdiven 2",
    "192.168.1.101": "Teknik Oda",
    "192.168.1.102": "Trafo Merkezi",
}

KAMERA_AYAR_DOSYASI = "cameras.json"
GUNCEL_ARIZA_DOSYASI = "arizali_kameralar_guncel.txt"
VARSAYILAN_GRUP = "Grupsuz"
TUM_GRUPLAR_FILTRE = "[Tüm Gruplar]"
OTOMATIK_KURAL_SECIMI = "⚡ Otomatik (IP Blok Kurallarına Göre Dağıt)"


@dataclass
class MonitorConfig:
    rtsp_stream_path: str = "stream1"
    rtsp_port: int = 554
    ping_timeout_ms: int = 1000
    sample_frame_count: int = 4
    frame_interval_sec: float = 0.3
    consecutive_fail_limit: int = 3
    check_interval_sec: int = 30
    max_worker_threads: int = 12
    log_dir: str = "logs"


@dataclass
class CheckResult:
    ip: str
    is_ping_ok: bool
    is_stream_ok: bool
    is_frozen: bool
    error_message: str = ""
    ping_time_ms: float = 0.0

    @property
    def is_healthy(self) -> bool:
        return self.is_ping_ok and self.is_stream_ok and not self.is_frozen


@dataclass
class CameraState:
    ip: str
    name: str = ""
    consecutive_failures: int = 0
    status: str = "ONLINE"
    last_error: str = ""
    first_failure_time: Optional[datetime] = None
    last_check_time: Optional[datetime] = None
    group: str = VARSAYILAN_GRUP


def match_group_for_ip(ip: str, block_rules: Dict[str, str], default_group: str = VARSAYILAN_GRUP) -> str:
    """Bir IP adresinin IP bloğu kurallarına göre hangi istasyon/gruba ait olduğunu tespit eder."""
    parts = ip.split(".")
    for pattern, group_name in block_rules.items():
        pat = pattern.strip()
        if ip.startswith(pat) or (not pat.endswith(".") and ip.startswith(pat + ".")):
            return group_name
        if len(parts) >= 3 and pat == parts[2]:
            return group_name
        if f".{pat}." in ip or ip.startswith(f"{pat}."):
            return group_name
    return default_group


def extract_ips_and_names(text: str) -> List[Tuple[str, str]]:
    """Metin içerisinden IPv4 adreslerini ve varsa yanındaki kamera adlarını ayrıştırır.
    Desteklenen formatlar:
    - 172.16.45.55, Turnike Giriş
    - 172.16.45.55; Peron 1
    - 172.16.45.55 - Gişe Önü
    - 172.16.45.55  Yürüyen Merdiven
    - 172.16.45.55 (isimsiz)
    """
    pattern = r"\b(?:[0-9]{1,3}\.){3}[0-9]{1,3}\b"
    results: List[Tuple[str, str]] = []
    seen_ips = set()

    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        match = re.search(pattern, line)
        if match:
            ip = match.group(0)
            octets = ip.split(".")
            if all(0 <= int(o) <= 255 for o in octets):
                if ip not in seen_ips:
                    seen_ips.add(ip)
                    # IP'den sonraki kısmı isim olarak al
                    after_ip = line[match.end():].strip()
                    # Başındaki ayraçları temizle (, ; - : | tab)
                    clean_name = re.sub(r"^[\s,;:|\-]+", "", after_ip).strip()
                    results.append((ip, clean_name))

    return results


def extract_valid_ips(text: str) -> List[str]:
    pairs = extract_ips_and_names(text)
    return [p[0] for p in pairs]


# ============================================================================
# EXCEL VE RAPOR DIŞA AKTARMA MOTORU (SIFIR HARİCİ BAĞIMLILIK / %100 OFFLINE)
# ============================================================================
class ExcelReportExporter:
    """Harici paket (openpyxl vb.) gerektirmeyen, standart Python ile Microsoft Excel
    tarafından doğrudan açılabilen profesyonel renkli Excel (.xls / XML) ve CSV üreten motor.
    """

    @staticmethod
    def _escape_xml(val: str) -> str:
        return (
            str(val)
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
            .replace("'", "&apos;")
        )

    @classmethod
    def export_to_excel_xml(
        cls,
        target_path: str,
        states: Dict[str, CameraState],
        results: Optional[Dict[str, CheckResult]] = None,
        only_faults: bool = False,
    ) -> int:
        """Microsoft XML Spreadsheet 2003 (.xls) formatında renkli ve biçimli Excel tablosu üretir."""
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

        if only_faults:
            export_cams = [s for s in states.values() if s.status in ("ARIZALI", "UYARI")]
        else:
            export_cams = list(states.values())

        export_cams.sort(key=lambda x: (x.group, x.ip))

        xml_lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<?mso-application progid="Excel.Sheet"?>',
            '<Workbook xmlns="urn:schemas-microsoft-com:office:spreadsheet"',
            ' xmlns:o="urn:schemas-microsoft-com:office:office"',
            ' xmlns:x="urn:schemas-microsoft-com:office:excel"',
            ' xmlns:ss="urn:schemas-microsoft-com:office:spreadsheet"',
            ' xmlns:html="http://www.w3.org/TR/REC-html40">',
            ' <Styles>',
            '  <Style ss:ID="Default" ss:Name="Normal">',
            '   <Alignment ss:Vertical="Center"/>',
            '   <Borders/>',
            '   <Font ss:FontName="Segoe UI" ss:Size="10" ss:Color="#0F172A"/>',
            '  </Style>',
            '  <Style ss:ID="MainTitle">',
            '   <Alignment ss:Horizontal="Center" ss:Vertical="Center"/>',
            '   <Font ss:FontName="Segoe UI" ss:Size="14" ss:Bold="1" ss:Color="#FFFFFF"/>',
            '   <Interior ss:Color="#0F172A" ss:Pattern="Solid"/>',
            '  </Style>',
            '  <Style ss:ID="SubTitle">',
            '   <Alignment ss:Horizontal="Center" ss:Vertical="Center"/>',
            '   <Font ss:FontName="Segoe UI" ss:Size="9" ss:Italic="1" ss:Color="#CBD5E1"/>',
            '   <Interior ss:Color="#1E293B" ss:Pattern="Solid"/>',
            '  </Style>',
            '  <Style ss:ID="ColHeader">',
            '   <Alignment ss:Horizontal="Center" ss:Vertical="Center"/>',
            '   <Borders>',
            '    <Border ss:Position="Bottom" ss:LineStyle="Continuous" ss:Weight="2" ss:Color="#0369A1"/>',
            '   </Borders>',
            '   <Font ss:FontName="Segoe UI" ss:Size="10" ss:Bold="1" ss:Color="#FFFFFF"/>',
            '   <Interior ss:Color="#0284C7" ss:Pattern="Solid"/>',
            '  </Style>',
            '  <Style ss:ID="RowFault">',
            '   <Alignment ss:Vertical="Center"/>',
            '   <Borders>',
            '    <Border ss:Position="Bottom" ss:LineStyle="Continuous" ss:Weight="1" ss:Color="#FCA5A5"/>',
            '   </Borders>',
            '   <Font ss:FontName="Segoe UI" ss:Size="10" ss:Color="#991B1B" ss:Bold="1"/>',
            '   <Interior ss:Color="#FEE2E2" ss:Pattern="Solid"/>',
            '  </Style>',
            '  <Style ss:ID="RowWarn">',
            '   <Alignment ss:Vertical="Center"/>',
            '   <Borders>',
            '    <Border ss:Position="Bottom" ss:LineStyle="Continuous" ss:Weight="1" ss:Color="#FCD34D"/>',
            '   </Borders>',
            '   <Font ss:FontName="Segoe UI" ss:Size="10" ss:Color="#92400E" ss:Bold="1"/>',
            '   <Interior ss:Color="#FEF3C7" ss:Pattern="Solid"/>',
            '  </Style>',
            '  <Style ss:ID="RowOnline">',
            '   <Alignment ss:Vertical="Center"/>',
            '   <Borders>',
            '    <Border ss:Position="Bottom" ss:LineStyle="Continuous" ss:Weight="1" ss:Color="#86EFAC"/>',
            '   </Borders>',
            '   <Font ss:FontName="Segoe UI" ss:Size="10" ss:Color="#166534"/>',
            '   <Interior ss:Color="#DCFCE7" ss:Pattern="Solid"/>',
            '  </Style>',
            ' </Styles>',
            ' <Worksheet ss:Name="Kamera Raporu">',
            '  <Table ss:DefaultRowHeight="20">',
            '   <Column ss:Width="45"/>',    # S.No
            '   <Column ss:Width="115"/>',   # IP
            '   <Column ss:Width="150"/>',   # Kamera Adı / Konum
            '   <Column ss:Width="135"/>',   # İstasyon / Grup
            '   <Column ss:Width="85"/>',    # Durum
            '   <Column ss:Width="75"/>',    # Ping
            '   <Column ss:Width="80"/>',    # Sayaç
            '   <Column ss:Width="175"/>',   # Arıza Türü
            '   <Column ss:Width="130"/>',   # Başlangıç
            '   <Column ss:Width="95"/>',    # Son Kontrol
            '   <Column ss:Width="270"/>',   # Detay
        ]

        title_text = "ANKARA METRO - ARIZALI KAMERA RAPORU" if only_faults else "ANKARA METRO - TÜM KAMERALAR DURUM RAPORU"
        xml_lines.extend([
            '   <Row ss:Height="30">',
            f'    <Cell ss:MergeAcross="10" ss:StyleID="MainTitle"><Data ss:Type="String">{cls._escape_xml(title_text)}</Data></Cell>',
            '   </Row>',
            '   <Row ss:Height="20">',
            f'    <Cell ss:MergeAcross="10" ss:StyleID="SubTitle"><Data ss:Type="String">Rapor Tarihi: {now_str} | Toplam Listelenen Kamera: {len(export_cams)}</Data></Cell>',
            '   </Row>',
            '   <Row ss:Height="24">',
            '    <Cell ss:StyleID="ColHeader"><Data ss:Type="String">S.No</Data></Cell>',
            '    <Cell ss:StyleID="ColHeader"><Data ss:Type="String">Kamera IP</Data></Cell>',
            '    <Cell ss:StyleID="ColHeader"><Data ss:Type="String">Kamera Adı / Konum</Data></Cell>',
            '    <Cell ss:StyleID="ColHeader"><Data ss:Type="String">İstasyon / Grup</Data></Cell>',
            '    <Cell ss:StyleID="ColHeader"><Data ss:Type="String">Durum</Data></Cell>',
            '    <Cell ss:StyleID="ColHeader"><Data ss:Type="String">Ping (ms)</Data></Cell>',
            '    <Cell ss:StyleID="ColHeader"><Data ss:Type="String">Hata Sayacı</Data></Cell>',
            '    <Cell ss:StyleID="ColHeader"><Data ss:Type="String">Arıza Türü</Data></Cell>',
            '    <Cell ss:StyleID="ColHeader"><Data ss:Type="String">Arıza Başlangıcı</Data></Cell>',
            '    <Cell ss:StyleID="ColHeader"><Data ss:Type="String">Son Kontrol</Data></Cell>',
            '    <Cell ss:StyleID="ColHeader"><Data ss:Type="String">Detaylı Teşhis / Hata Mesajı</Data></Cell>',
            '   </Row>',
        ])

        for idx, st in enumerate(export_cams, 1):
            res = results.get(st.ip) if results else None
            style_id = "RowFault" if st.status == "ARIZALI" else ("RowWarn" if st.status == "UYARI" else "RowOnline")

            err_lower = st.last_error.lower()
            if "ping" in err_lower or "eriş" in err_lower:
                fault_type = "Ağ Bağlantısı Kesildi (Ping Yok)"
            elif "dondu" in err_lower or "frozen" in err_lower:
                fault_type = "Görüntü Dondu (Frozen Frame)"
            elif st.status in ("ARIZALI", "UYARI"):
                fault_type = "RTSP Video Sinyali Yok"
            else:
                fault_type = "Sorunsuz (Aktif)"

            ping_str = f"{res.ping_time_ms:.1f}" if res and res.is_ping_ok else ("Zaman Aşımı" if st.status != "ONLINE" else "OK")
            start_str = st.first_failure_time.strftime("%Y-%m-%d %H:%M:%S") if st.first_failure_time else "-"
            last_chk_str = st.last_check_time.strftime("%H:%M:%S") if st.last_check_time else "-"
            desc_str = st.last_error if st.last_error else "Sağlıklı (Canlı video akışı devam ediyor)"
            cam_name_str = st.name if st.name else "-"

            xml_lines.extend([
                '   <Row ss:Height="22">',
                f'    <Cell ss:StyleID="{style_id}"><Data ss:Type="Number">{idx}</Data></Cell>',
                f'    <Cell ss:StyleID="{style_id}"><Data ss:Type="String">{cls._escape_xml(st.ip)}</Data></Cell>',
                f'    <Cell ss:StyleID="{style_id}"><Data ss:Type="String">{cls._escape_xml(cam_name_str)}</Data></Cell>',
                f'    <Cell ss:StyleID="{style_id}"><Data ss:Type="String">{cls._escape_xml(st.group)}</Data></Cell>',
                f'    <Cell ss:StyleID="{style_id}"><Data ss:Type="String">{st.status}</Data></Cell>',
                f'    <Cell ss:StyleID="{style_id}"><Data ss:Type="String">{cls._escape_xml(ping_str)}</Data></Cell>',
                f'    <Cell ss:StyleID="{style_id}"><Data ss:Type="String">{st.consecutive_failures}/3</Data></Cell>',
                f'    <Cell ss:StyleID="{style_id}"><Data ss:Type="String">{cls._escape_xml(fault_type)}</Data></Cell>',
                f'    <Cell ss:StyleID="{style_id}"><Data ss:Type="String">{cls._escape_xml(start_str)}</Data></Cell>',
                f'    <Cell ss:StyleID="{style_id}"><Data ss:Type="String">{cls._escape_xml(last_chk_str)}</Data></Cell>',
                f'    <Cell ss:StyleID="{style_id}"><Data ss:Type="String">{cls._escape_xml(desc_str)}</Data></Cell>',
                '   </Row>',
            ])

        xml_lines.extend([
            '  </Table>',
            ' </Worksheet>',
            '</Workbook>',
        ])

        with open(target_path, "w", encoding="utf-8") as f:
            f.write("\n".join(xml_lines))

        return len(export_cams)

    @classmethod
    def export_to_csv(
        cls,
        target_path: str,
        states: Dict[str, CameraState],
        results: Optional[Dict[str, CheckResult]] = None,
        only_faults: bool = False,
    ) -> int:
        """Türkçe Windows Excel standartlarına tam uyumlu UTF-8-SIG noktalı virgüllü CSV üretir."""
        if only_faults:
            export_cams = [s for s in states.values() if s.status in ("ARIZALI", "UYARI")]
        else:
            export_cams = list(states.values())

        export_cams.sort(key=lambda x: (x.group, x.ip))

        headers = [
            "Sıra No",
            "Kamera IP",
            "Kamera Adı / Konum",
            "İstasyon / Grup",
            "Durum",
            "Ping (ms)",
            "Hata Sayacı",
            "Arıza Türü",
            "Arıza Başlangıcı",
            "Son Kontrol",
            "Detaylı Teşhis / Hata Mesajı",
        ]

        with open(target_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv.writer(f, delimiter=";")
            writer.writerow(headers)

            for idx, st in enumerate(export_cams, 1):
                res = results.get(st.ip) if results else None
                err_lower = st.last_error.lower()
                if "ping" in err_lower or "eriş" in err_lower:
                    fault_type = "Ağ Bağlantısı Kesildi (Ping Yok)"
                elif "dondu" in err_lower or "frozen" in err_lower:
                    fault_type = "Görüntü Dondu (Frozen Frame)"
                elif st.status in ("ARIZALI", "UYARI"):
                    fault_type = "RTSP Video Sinyali Yok"
                else:
                    fault_type = "Sorunsuz (Aktif)"

                ping_str = f"{res.ping_time_ms:.1f}" if res and res.is_ping_ok else ("Zaman Aşımı" if st.status != "ONLINE" else "OK")
                start_str = st.first_failure_time.strftime("%Y-%m-%d %H:%M:%S") if st.first_failure_time else "-"
                last_chk_str = st.last_check_time.strftime("%H:%M:%S") if st.last_check_time else "-"
                desc_str = st.last_error if st.last_error else "Sağlıklı (Canlı video akışı devam ediyor)"
                cam_name_str = st.name if st.name else "-"

                writer.writerow([
                    idx,
                    st.ip,
                    cam_name_str,
                    st.group,
                    st.status,
                    ping_str,
                    f"{st.consecutive_failures}/3",
                    fault_type,
                    start_str,
                    last_chk_str,
                    desc_str,
                ])

        return len(export_cams)


class CleanFaultLogger:
    def __init__(self, log_dir: str = "logs"):
        self.log_dir = log_dir
        self._lock = threading.Lock()
        os.makedirs(self.log_dir, exist_ok=True)
        if hasattr(sys.stdout, "reconfigure"):
            try:
                sys.stdout.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass

    def _get_daily_log_path(self) -> str:
        date_str = datetime.now().strftime("%Y_%m_%d")
        return os.path.join(self.log_dir, f"kamera_ariza_{date_str}.log")

    def _get_active_faults_path(self) -> str:
        return os.path.join(self.log_dir, GUNCEL_ARIZA_DOSYASI)

    def log_fault(self, ip: str, error_msg: str, fail_count: int,
                  first_time: Optional[datetime] = None,
                  group: str = VARSAYILAN_GRUP,
                  name: str = "") -> None:
        now = datetime.now()
        now_str = now.strftime("%Y-%m-%d %H:%M:%S")
        start_str = first_time.strftime("%Y-%m-%d %H:%M:%S") if first_time else now_str
        if "ping" in error_msg.lower() or "eriş" in error_msg.lower() or "eris" in error_msg.lower():
            fault_type = "AĞ BAĞLANTISI YOK (ICMP Ping Zaman Aşımı / Sinyal Yok)"
        elif "dondu" in error_msg.lower() or "frozen" in error_msg.lower():
            fault_type = "GÖRÜNTÜ DONDU (Frozen Frame / RTSP Piksel Farkı 0)"
        else:
            fault_type = "RTSP AKIŞ HATASI (Video Sinyali Kesildi)"

        name_display = f"{name} ({ip})" if name else ip

        block = (
            "================================================================================\n"
            f"[ARIZA TESPİT EDİLDİ] - {now_str}\n"
            f"Kamera          : {name_display}\n"
            f"Grup / İstasyon : {group}\n"
            f"Arıza Türü      : {fault_type}\n"
            f"Detaylı Açıklama: {error_msg}\n"
            f"Hata Durumu     : Üst üste {fail_count} denetim başarısız oldu\n"
            f"Arıza Başlangıcı: {start_str}\n"
            "================================================================================\n\n"
        )
        with self._lock:
            try:
                with open(self._get_daily_log_path(), "a", encoding="utf-8") as f:
                    f.write(block)
                print(f"[{now_str}] [ARIZALI] Kamera: {name_display} [{group}] | Tür: {fault_type}")
            except Exception as e:
                print(f"Log yazma hatası: {e}")

    def log_recovery(self, ip: str, ping_ms: float, group: str = VARSAYILAN_GRUP, name: str = "") -> None:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        name_display = f"{name} ({ip})" if name else ip
        block = (
            "--------------------------------------------------------------------------------\n"
            f"[ARIZA GİDERİLDİ / KURTARILDI] - {now_str}\n"
            f"Kamera          : {name_display}\n"
            f"Grup / İstasyon : {group}\n"
            "Yeni Durum      : ÇEVRİMİÇİ (ONLINE)\n"
            f"Ping Yanıtı     : {ping_ms:.1f} ms\n"
            "RTSP Akışı      : Aktif ve Canlı\n"
            "--------------------------------------------------------------------------------\n\n"
        )
        with self._lock:
            try:
                with open(self._get_daily_log_path(), "a", encoding="utf-8") as f:
                    f.write(block)
                print(f"[{now_str}] [KURTARILDI] Kamera: {name_display} [{group}] normale döndü.")
            except Exception as e:
                print(f"Log yazma hatası: {e}")

    def update_active_faults_summary(self, states: Dict[str, CameraState]) -> None:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        faulty_cameras = [s for s in states.values() if s.status == "ARIZALI"]
        warning_cameras = [s for s in states.values() if s.status == "UYARI"]
        lines = [
            "================================================================================",
            "             ANKARA METRO - ANLIK ARIZALI KAMERA DURUM RAPORU",
            f"             Rapor Tarihi: {now_str}",
            "================================================================================",
            f"Toplam İzlenen Kamera : {len(states)}",
            f"Aktif Çalışan Kamera  : {len(states) - len(faulty_cameras) - len(warning_cameras)}",
            f"Kritik Arızalı Kamera : {len(faulty_cameras)}",
            f"Uyarıdaki Kameralar   : {len(warning_cameras)}",
            "--------------------------------------------------------------------------------",
            "KRİTİK ARIZALI KAMERALAR LİSTESİ:",
            "--------------------------------------------------------------------------------",
        ]
        if not faulty_cameras:
            lines.append("  Tebrikler! Şu anda sistemde hiçbir arızalı kamera bulunmuyor. Tüm sistem aktif.")
        else:
            for idx, c in enumerate(faulty_cameras, 1):
                start_str = c.first_failure_time.strftime("%H:%M:%S") if c.first_failure_time else "Bilinmiyor"
                name_part = f"{c.name:<18}" if c.name else f"{'-':<18}"
                lines.append(
                    f" {idx:2d}) IP: {c.ip:<15} | Adı: {name_part} | Grup: {c.group:<18} | "
                    f"Hata: {c.last_error:<28} | Başlangıç: {start_str}"
                )
        lines.extend([
            "--------------------------------------------------------------------------------",
            "Not: Bu dosya her tarama döngüsünde otomatik olarak güncellenir.",
            "================================================================================\n"
        ])
        with self._lock:
            try:
                with open(self._get_active_faults_path(), "w", encoding="utf-8") as f:
                    f.write("\n".join(lines))
            except Exception:
                pass

    def log_scan_cycle(self, states: Dict[str, CameraState],
                       results: Optional[Dict[str, CheckResult]] = None) -> None:
        now_str = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        total = len(states)
        online = sum(1 for s in states.values() if s.status == "ONLINE")
        warning = sum(1 for s in states.values() if s.status == "UYARI")
        faulty = sum(1 for s in states.values() if s.status == "ARIZALI")
        lines = [
            "--------------------------------------------------------------------------------",
            f"[TARAMA DÖNGÜSÜ] - {now_str}",
            f"Toplam Kamera: {total} | Çevrimiçi: {online} | Uyarı: {warning} | Arızalı: {faulty}",
            "--------------------------------------------------------------------------------",
        ]
        groups_map: Dict[str, List[CameraState]] = {}
        for ip in sorted(states.keys()):
            st = states[ip]
            groups_map.setdefault(st.group, []).append(st)
        for group_name in sorted(groups_map.keys()):
            lines.append(f"  [ {group_name} ]")
            for st in groups_map[group_name]:
                ip = st.ip
                res = results.get(ip) if results else None
                name_info = f"({st.name}) " if st.name else ""
                if st.status == "ONLINE":
                    ping_str = f"Ping: {res.ping_time_ms:.1f}ms" if res and res.is_ping_ok else "Ping: OK"
                    lines.append(f"    [ONLINE]  {ip:<16} {name_info:<20} | {ping_str:<14} | RTSP Akış: Aktif (Canlı)")
                elif st.status == "UYARI":
                    lines.append(f"    [UYARI]   {ip:<16} {name_info:<20} | Sayaç: {st.consecutive_failures}/3 | Sebep: {st.last_error}")
                else:
                    lines.append(f"    [ARIZALI] {ip:<16} {name_info:<20} | Sayaç: {st.consecutive_failures}/3 (EŞİK AŞILDI) | HATA: {st.last_error}")
        lines.append("--------------------------------------------------------------------------------\n")
        block = "\n".join(lines)
        with self._lock:
            try:
                with open(self._get_daily_log_path(), "a", encoding="utf-8") as f:
                    f.write(block)
            except Exception as e:
                print(f"Döngü log yazma hatası: {e}")


class NetworkChecker:
    @staticmethod
    def ping(ip: str, timeout_ms: int = 1000) -> Tuple[bool, float, str]:
        is_windows = platform.system().lower() == "windows"
        if is_windows:
            cmd = ["ping", "-n", "1", "-w", str(timeout_ms), ip]
            creation_flags = subprocess.CREATE_NO_WINDOW if hasattr(subprocess, "CREATE_NO_WINDOW") else 0
        else:
            timeout_sec = max(1, int(timeout_ms / 1000))
            cmd = ["ping", "-c", "1", "-W", str(timeout_sec), ip]
            creation_flags = 0
        start_time = time.perf_counter()
        try:
            result = subprocess.run(
                cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                timeout=(timeout_ms / 1000.0) + 1.0, creationflags=creation_flags
            )
            elapsed_ms = (time.perf_counter() - start_time) * 1000.0
            if result.returncode == 0:
                return True, elapsed_ms, ""
            return False, elapsed_ms, "Ağ yanıtı yok (ICMP Ping başarısız/Erişilemez)"
        except subprocess.TimeoutExpired:
            return False, float(timeout_ms), f"Ping zaman aşımı ({timeout_ms} ms aşıldı)"
        except Exception as ex:
            return False, 0.0, f"Ping hatası: {str(ex)}"


class VideoStreamAnalyzer:
    def __init__(self, config: MonitorConfig):
        self.config = config

    def build_rtsp_url(self, ip: str) -> str:
        return f"rtsp://{ip}/{self.config.rtsp_stream_path}"

    def analyze_stream(self, ip: str) -> Tuple[bool, bool, str]:
        if cv2 is None:
            return False, False, "opencv-python (cv2) modülü kurulu değil"
        rtsp_url = self.build_rtsp_url(ip)
        cap = None
        try:
            cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
            cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            if not cap.isOpened():
                return False, False, f"RTSP akışına bağlanılamadı ({rtsp_url})"
            frames_gray = []
            for idx in range(self.config.sample_frame_count):
                ret, frame = cap.read()
                if not ret or frame is None:
                    return False, False, f"Video sinyali kesildi (Kare #{idx + 1} okunamadı)"
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                frames_gray.append(gray)
                if idx < self.config.sample_frame_count - 1:
                    time.sleep(self.config.frame_interval_sec)
            all_differences_zero = True
            for i in range(len(frames_gray) - 1):
                diff = cv2.absdiff(frames_gray[i], frames_gray[i + 1])
                if cv2.countNonZero(diff) > 0:
                    all_differences_zero = False
                    break
            if all_differences_zero:
                return True, True, "Görüntü dondu (Frozen Frame: Piksel farkı sıfır)"
            return True, False, ""
        except Exception as ex:
            return False, False, f"RTSP analizi hatası: {str(ex)}"
        finally:
            if cap is not None:
                cap.release()


ANKARA_METRO_DARK_STYLE = """
QMainWindow, QWidget {
    background-color: #0f172a;
    color: #f1f5f9;
    font-family: 'Segoe UI', 'SF Pro Display', Roboto, sans-serif;
    font-size: 13px;
}
QFrame.CardFrame {
    background-color: #1e293b;
    border: 1px solid #334155;
    border-radius: 10px;
}
QPushButton {
    background-color: #334155;
    color: #f8fafc;
    border: 1px solid #475569;
    border-radius: 6px;
    padding: 7px 14px;
    font-weight: 600;
}
QPushButton:hover { background-color: #475569; border-color: #64748b; }
QPushButton:pressed { background-color: #1e293b; }
QPushButton.PrimaryBtn { background-color: #0284c7; border: 1px solid #38bdf8; }
QPushButton.PrimaryBtn:hover { background-color: #0369a1; }
QPushButton.SuccessBtn { background-color: #15803d; border: 1px solid #22c55e; }
QPushButton.SuccessBtn:hover { background-color: #166534; }
QPushButton.DangerBtn { background-color: #b91c1c; border: 1px solid #ef4444; }
QPushButton.DangerBtn:hover { background-color: #991b1b; }
QPushButton.WarningBtn { background-color: #b45309; border: 1px solid #f59e0b; }
QPushButton.GroupBtn { background-color: #4c1d95; border: 1px solid #8b5cf6; color: #ddd6fe; }
QPushButton.GroupBtn:hover { background-color: #5b21b6; }
QPushButton.ExcelBtn { background-color: #065f46; border: 1px solid #10b981; color: #a7f3d0; font-weight: bold; }
QPushButton.ExcelBtn:hover { background-color: #047857; }
QTableWidget {
    background-color: #1e293b;
    gridline-color: #334155;
    border: 1px solid #334155;
    border-radius: 8px;
    selection-background-color: #0369a1;
    selection-color: #ffffff;
}
QTableWidget::item { padding: 6px 10px; border-bottom: 1px solid #334155; }
QHeaderView::section {
    background-color: #0f172a;
    color: #94a3b8;
    padding: 8px 10px;
    border: none;
    border-bottom: 2px solid #334155;
    font-weight: bold;
    font-size: 12px;
}
QSpinBox, QLineEdit, QTextEdit {
    background-color: #090d16;
    color: #f8fafc;
    border: 1px solid #334155;
    border-radius: 6px;
    padding: 6px 10px;
}
QSpinBox:focus, QLineEdit:focus, QTextEdit:focus { border: 1px solid #38bdf8; }
QComboBox {
    background-color: #090d16;
    color: #f8fafc;
    border: 1px solid #334155;
    border-radius: 6px;
    padding: 5px 10px;
    min-width: 150px;
}
QComboBox:focus { border: 1px solid #8b5cf6; }
QComboBox::drop-down { border: none; width: 22px; }
QComboBox QAbstractItemView {
    background-color: #1e293b;
    color: #f8fafc;
    selection-background-color: #4c1d95;
    border: 1px solid #334155;
}
QListWidget {
    background-color: #090d16;
    color: #f8fafc;
    border: 1px solid #334155;
    border-radius: 6px;
}
QListWidget::item { padding: 6px 10px; }
QListWidget::item:selected { background-color: #4c1d95; color: #ddd6fe; }
QListWidget::item:hover { background-color: #1e293b; }
QRadioButton { color: #cbd5e1; font-weight: bold; spacing: 6px; }
QScrollBar:vertical { border: none; background: #0f172a; width: 8px; border-radius: 4px; }
QScrollBar::handle:vertical { background: #334155; min-height: 20px; border-radius: 4px; }
"""

if PYQT5_AVAILABLE:

    class BlockRulesDialog(QDialog):
        """IP Bloğu / İstasyon Eşleştirme Kurallarını Düzenleme Penceresi."""

        def __init__(self, block_rules: Dict[str, str], group_names: List[str], parent=None):
            super().__init__(parent)
            self.block_rules = copy.deepcopy(block_rules)
            self.group_names = group_names
            self.setWindowTitle("⚡ IP Bloğu - İstasyon Eşleştirme Kuralları")
            self.resize(650, 450)
            self.setStyleSheet(ANKARA_METRO_DARK_STYLE)

            layout = QVBoxLayout(self)
            layout.setContentsMargins(16, 16, 16, 16)
            layout.setSpacing(12)

            info = QLabel(
                "<b>İstasyon IP Bloğu Otomasyonu:</b><br>"
                "Belirli bir IP bloğundaki kameraların otomatik olarak ilgili istasyon grubuna atanmasını sağlar.<br>"
                "<i>Örnek 1:</i> <b>172.16.45.</b> (veya sadece <b>45</b>) ➜ <b>Milli Kütüphane</b><br>"
                "<i>Örnek 2:</i> <b>172.16.47.</b> (veya sadece <b>47</b>) ➜ <b>Ümitköy</b>"
            )
            info.setTextFormat(Qt.RichText)
            layout.addWidget(info)

            self.rules_table = QTableWidget()
            self.rules_table.setColumnCount(3)
            self.rules_table.setHorizontalHeaderLabels(["IP Bloğu / Deseni", "Atanacak İstasyon / Grup", "İşlem"])
            self.rules_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
            self.rules_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
            self.rules_table.setColumnWidth(2, 90)
            layout.addWidget(self.rules_table)

            add_box = QFrame()
            add_box.setProperty("class", "CardFrame")
            add_layout = QHBoxLayout(add_box)
            add_layout.setContentsMargins(10, 8, 10, 8)

            add_layout.addWidget(QLabel("IP Bloğu:"))
            self.pattern_input = QLineEdit()
            self.pattern_input.setPlaceholderText("Örn: 172.16.45. veya 45")
            add_layout.addWidget(self.pattern_input)

            add_layout.addWidget(QLabel("İstasyon / Grup:"))
            self.group_input = QLineEdit()
            self.group_input.setPlaceholderText("Örn: Milli Kütüphane")
            add_layout.addWidget(self.group_input)

            add_btn = QPushButton("➕ Kural Ekle")
            add_btn.setProperty("class", "SuccessBtn")
            add_btn.clicked.connect(self.add_rule)
            add_layout.addWidget(add_btn)

            layout.addWidget(add_box)

            btn_row = QHBoxLayout()
            cancel_btn = QPushButton("İptal")
            cancel_btn.clicked.connect(self.reject)
            btn_row.addWidget(cancel_btn)
            btn_row.addStretch()

            save_btn = QPushButton("💾 Kuralları Kaydet")
            save_btn.setProperty("class", "PrimaryBtn")
            save_btn.clicked.connect(self.accept)
            btn_row.addWidget(save_btn)
            layout.addLayout(btn_row)

            self.refresh_table()

        def refresh_table(self) -> None:
            self.rules_table.setRowCount(len(self.block_rules))
            for row, (pattern, grp) in enumerate(sorted(self.block_rules.items())):
                p_item = QTableWidgetItem(pattern)
                p_item.setTextAlignment(Qt.AlignCenter)
                self.rules_table.setItem(row, 0, p_item)

                g_item = QTableWidgetItem(grp)
                g_item.setTextAlignment(Qt.AlignCenter)
                g_item.setForeground(QColor("#a78bfa"))
                self.rules_table.setItem(row, 1, g_item)

                del_btn = QPushButton("Sil")
                del_btn.setProperty("class", "DangerBtn")
                del_btn.clicked.connect(lambda checked, pat=pattern: self.delete_rule(pat))
                self.rules_table.setCellWidget(row, 2, del_btn)

        def add_rule(self) -> None:
            pat = self.pattern_input.text().strip()
            grp = self.group_input.text().strip()
            if not pat or not grp:
                QMessageBox.warning(self, "Uyarı", "Lütfen hem IP Bloğu hem de İstasyon / Grup adını giriniz.")
                return
            self.block_rules[pat] = grp
            self.pattern_input.clear()
            self.group_input.clear()
            self.refresh_table()

        def delete_rule(self, pattern: str) -> None:
            if pattern in self.block_rules:
                del self.block_rules[pattern]
                self.refresh_table()


    class BulkImportDialog(QDialog):
        """Toplu IP adresi içe aktarma penceresi (Kamera Adı ve Blok Kuralları Destekli)."""

        def __init__(self, existing_ips: List[str], group_names: List[str], block_rules: Dict[str, str], parent=None):
            super().__init__(parent)
            self.existing_ips = set(existing_ips)
            self.block_rules = block_rules
            self.imported_pairs: List[Tuple[str, str]] = []  # [(ip, name), ...]
            self.ip_to_group_map: Dict[str, str] = {}
            self.selected_mode: str = OTOMATIK_KURAL_SECIMI

            self.setWindowTitle("Toplu Kamera IP ve İsim İçe Aktar (Bulk Import)")
            self.resize(700, 620)
            self.setStyleSheet(ANKARA_METRO_DARK_STYLE)

            layout = QVBoxLayout(self)
            layout.setContentsMargins(18, 18, 18, 18)
            layout.setSpacing(12)

            info_lbl = QLabel(
                "<b>Kamera IP ve İsimlerini İçe Aktarın:</b><br>"
                "Excel'den, Not Defteri'nden kopyalayabilir ya da <b>.TXT / .CSV</b> dosyası seçebilirsiniz.<br>"
                "<span style='color:#38bdf8;'>💡 İpucu:</span> IP yanına kamera adı da yazabilirsiniz: "
                "<i>Örn: 172.16.45.55, Turnike Giriş 1</i> veya <i>172.16.45.56; Peron Doğu</i><br>"
                "<span style='color:#a78bfa;'>⚡ IP Blok Kuralları etkindir:</span> 45 bloğu otomatik 'Milli Kütüphane', 47 bloğu otomatik 'Ümitköy' grubuna atanır."
            )
            info_lbl.setTextFormat(Qt.RichText)
            layout.addWidget(info_lbl)

            btn_box = QHBoxLayout()
            self.file_btn = QPushButton("📂 TXT / CSV Dosyası Seç")
            self.file_btn.setProperty("class", "PrimaryBtn")
            self.file_btn.clicked.connect(self.select_file)
            btn_box.addWidget(self.file_btn)
            btn_box.addStretch()
            clear_btn = QPushButton("Temizle")
            clear_btn.clicked.connect(lambda: self.text_edit.clear())
            btn_box.addWidget(clear_btn)
            layout.addLayout(btn_box)

            self.text_edit = QTextEdit()
            self.text_edit.setPlaceholderText(
                "Örnek IP Listesi (İsimli veya İsimsiz):\n"
                "172.16.45.55, Turnike Giriş 1\n"
                "172.16.45.56, Peron Doğu\n"
                "172.16.47.55; Gişe Önü\n"
                "172.16.47.56 - Yürüyen Merdiven 2\n"
                "192.168.1.101\n..."
            )
            layout.addWidget(self.text_edit)

            group_frame = QFrame()
            group_frame.setProperty("class", "CardFrame")
            group_layout = QHBoxLayout(group_frame)
            group_layout.setContentsMargins(12, 10, 12, 10)

            grp_lbl = QLabel("📁 Grup Atama Yöntemi:")
            grp_lbl.setStyleSheet("color: #a78bfa; font-weight: bold;")
            group_layout.addWidget(grp_lbl)

            self.group_combo = QComboBox()
            self.group_combo.addItem(OTOMATIK_KURAL_SECIMI)
            for g in group_names:
                if g != VARSAYILAN_GRUP:
                    self.group_combo.addItem(f"Sabit Grup: {g}")
            self.group_combo.addItem(f"Sabit Grup: {VARSAYILAN_GRUP}")
            group_layout.addWidget(self.group_combo)

            group_layout.addStretch()
            layout.addWidget(group_frame)

            self.status_lbl = QLabel("")
            self.status_lbl.setStyleSheet("color: #38bdf8; font-weight: bold;")
            layout.addWidget(self.status_lbl)

            action_box = QHBoxLayout()
            cancel_btn = QPushButton("İptal")
            cancel_btn.clicked.connect(self.reject)
            action_box.addWidget(cancel_btn)
            action_box.addStretch()
            self.apply_btn = QPushButton("✅ Kameraları Listeye Ekle")
            self.apply_btn.setProperty("class", "SuccessBtn")
            self.apply_btn.clicked.connect(self.process_text)
            action_box.addWidget(self.apply_btn)
            layout.addLayout(action_box)

        def select_file(self) -> None:
            file_path, _ = QFileDialog.getOpenFileName(
                self, "Kamera IP Listesi Seç", "",
                "Metin Dosyaları (*.txt *.csv *.log);;Tüm Dosyalar (*.*)"
            )
            if file_path:
                try:
                    with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                        content = f.read()
                    self.text_edit.setText(content)
                    self.status_lbl.setText(f"Dosya yüklendi: {os.path.basename(file_path)}")
                except Exception as e:
                    QMessageBox.critical(self, "Hata", f"Dosya okunamadı: {e}")

        def process_text(self) -> None:
            raw_text = self.text_edit.toPlainText()
            found_pairs = extract_ips_and_names(raw_text)
            if not found_pairs:
                QMessageBox.warning(self, "Uyarı", "Metin içinde geçerli hiçbir IPv4 adresi bulunamadı!")
                return

            new_pairs = [p for p in found_pairs if p[0] not in self.existing_ips]
            duplicate_count = len(found_pairs) - len(new_pairs)
            if not new_pairs:
                QMessageBox.information(self, "Bilgi", f"Bulunan {len(found_pairs)} IP adresinin tamamı zaten sistemde mevcut.")
                return

            self.imported_pairs = new_pairs
            selected = self.group_combo.currentText()

            group_summary_counts: Dict[str, int] = {}
            named_count = 0
            for ip, name in new_pairs:
                if name:
                    named_count += 1
                if selected == OTOMATIK_KURAL_SECIMI:
                    target_g = match_group_for_ip(ip, self.block_rules, default_group=VARSAYILAN_GRUP)
                else:
                    target_g = selected.replace("Sabit Grup: ", "").strip()
                self.ip_to_group_map[ip] = target_g
                group_summary_counts[target_g] = group_summary_counts.get(target_g, 0) + 1

            summary_text = "\n".join([f"  • {grp}: {cnt} kamera" for grp, cnt in sorted(group_summary_counts.items())])
            QMessageBox.information(
                self, "Başarılı",
                f"Toplam {len(new_pairs)} adet yeni kamera sisteme eklendi! ({named_count} adedi isimli)\n\n"
                f"Grup Dağılımı:\n{summary_text}\n\n"
                f"({duplicate_count} adet mükerrer IP atlandı)"
            )
            self.accept()


    class GroupManagerDialog(QDialog):
        """Kamera gruplarını, istasyonları ve IP blok kurallarını yönetme penceresi."""

        def __init__(self, groups: Dict[str, List[str]], block_rules: Dict[str, str], parent=None):
            super().__init__(parent)
            self.groups: Dict[str, List[str]] = copy.deepcopy(groups)
            self.block_rules: Dict[str, str] = copy.deepcopy(block_rules)
            self.setWindowTitle("📁 Kamera Grubu & İstasyon Yönetimi")
            self.resize(920, 620)
            self.setStyleSheet(ANKARA_METRO_DARK_STYLE)

            self.setLayout(QVBoxLayout())
            self.layout().setContentsMargins(16, 16, 16, 16)
            self.layout().setSpacing(10)

            top_bar = QFrame()
            top_bar.setProperty("class", "CardFrame")
            top_bar_layout = QHBoxLayout(top_bar)
            top_bar_layout.setContentsMargins(12, 8, 12, 8)

            rule_btn = QPushButton("⚡ IP Bloğu Kurallarını Düzenle (45 ➜ Milli Kütüphane, 47 ➜ Ümitköy)")
            rule_btn.setProperty("class", "GroupBtn")
            rule_btn.clicked.connect(self.open_block_rules_dialog)
            top_bar_layout.addWidget(rule_btn)

            top_bar_layout.addSpacing(10)

            auto_distribute_btn = QPushButton("🔄 Tüm Kameraları Blok Kurallarına Göre Dağıt")
            auto_distribute_btn.setProperty("class", "PrimaryBtn")
            auto_distribute_btn.setToolTip("Sistemdeki tüm kameraları tanımlı IP blok kurallarına göre ilgili istasyon gruplarına taşır.")
            auto_distribute_btn.clicked.connect(self.auto_distribute_all_cameras)
            top_bar_layout.addWidget(auto_distribute_btn)

            top_bar_layout.addStretch()
            self.layout().addWidget(top_bar)

            inner_h = QHBoxLayout()

            # Sol Panel (Gruplar)
            left_panel = QWidget()
            left_layout = QVBoxLayout(left_panel)
            left_layout.setContentsMargins(0, 0, 0, 0)
            left_layout.setSpacing(8)
            grp_header = QLabel("İSTASYON / GRUP LİSTESİ")
            grp_header.setStyleSheet("color: #94a3b8; font-size: 11px; font-weight: bold;")
            left_layout.addWidget(grp_header)

            self.group_list = QListWidget()
            self.group_list.currentRowChanged.connect(self.on_group_selected)
            left_layout.addWidget(self.group_list)

            grp_btn_row = QHBoxLayout()
            add_grp_btn = QPushButton("➕ Yeni İstasyon")
            add_grp_btn.setProperty("class", "GroupBtn")
            add_grp_btn.clicked.connect(self.add_group)
            grp_btn_row.addWidget(add_grp_btn)

            rename_grp_btn = QPushButton("✏️ Ad Değiştir")
            rename_grp_btn.clicked.connect(self.rename_group)
            grp_btn_row.addWidget(rename_grp_btn)

            del_grp_btn = QPushButton("🗑 Sil")
            del_grp_btn.setProperty("class", "DangerBtn")
            del_grp_btn.clicked.connect(self.delete_group)
            grp_btn_row.addWidget(del_grp_btn)

            left_layout.addLayout(grp_btn_row)
            left_panel.setMaximumWidth(310)
            inner_h.addWidget(left_panel)

            # Orta Butonlar
            mid_panel = QWidget()
            mid_layout = QVBoxLayout(mid_panel)
            mid_layout.setContentsMargins(0, 0, 0, 0)
            mid_layout.setAlignment(Qt.AlignCenter)
            mid_panel.setMaximumWidth(70)

            move_right_btn = QPushButton("▶▶")
            move_right_btn.setToolTip("Seçili kameraları başka bir istasyona/gruba taşı")
            move_right_btn.clicked.connect(self.move_to_selected_group)
            move_right_btn.setMinimumHeight(42)

            move_left_btn = QPushButton("◀◀")
            move_left_btn.setToolTip("Seçili kameraları 'Grupsuz'a geri taşı")
            move_left_btn.clicked.connect(self.move_back_to_group)
            move_left_btn.setMinimumHeight(42)

            mid_layout.addStretch()
            mid_layout.addWidget(move_right_btn)
            mid_layout.addSpacing(10)
            mid_layout.addWidget(move_left_btn)
            mid_layout.addStretch()
            inner_h.addWidget(mid_panel)

            # Sağ Panel (Kameralar)
            right_panel = QWidget()
            right_layout = QVBoxLayout(right_panel)
            right_layout.setContentsMargins(0, 0, 0, 0)
            right_layout.setSpacing(8)

            cam_header_row = QHBoxLayout()
            self.cam_header_lbl = QLabel("KAMERALAR")
            self.cam_header_lbl.setStyleSheet("color: #94a3b8; font-size: 11px; font-weight: bold;")
            cam_header_row.addWidget(self.cam_header_lbl)
            cam_header_row.addStretch()
            right_layout.addLayout(cam_header_row)

            self.cam_list = QListWidget()
            self.cam_list.setSelectionMode(QListWidget.ExtendedSelection)
            right_layout.addWidget(self.cam_list)

            self.cam_count_lbl = QLabel("")
            self.cam_count_lbl.setStyleSheet("color: #64748b; font-size: 11px;")
            right_layout.addWidget(self.cam_count_lbl)
            inner_h.addWidget(right_panel)

            self.layout().addLayout(inner_h)

            sep = QFrame()
            sep.setFrameShape(QFrame.HLine)
            sep.setStyleSheet("background-color: #334155; max-height: 1px;")
            self.layout().addWidget(sep)

            action_row = QHBoxLayout()
            cancel_btn = QPushButton("İptal")
            cancel_btn.clicked.connect(self.reject)
            action_row.addWidget(cancel_btn)
            action_row.addStretch()

            save_btn = QPushButton("💾 Değişiklikleri Kaydet")
            save_btn.setProperty("class", "SuccessBtn")
            save_btn.setMinimumWidth(180)
            save_btn.clicked.connect(self.accept)
            action_row.addWidget(save_btn)
            self.layout().addLayout(action_row)

            self._refresh_group_list()

        def open_block_rules_dialog(self) -> None:
            dlg = BlockRulesDialog(self.block_rules, list(self.groups.keys()), self)
            if dlg.exec_() == QDialog.Accepted:
                self.block_rules = dlg.block_rules
                for grp in self.block_rules.values():
                    if grp not in self.groups:
                        self.groups[grp] = []
                self._refresh_group_list()
                QMessageBox.information(self, "Bilgi", "IP bloğu kuralları güncellendi.")

        def auto_distribute_all_cameras(self) -> None:
            if not self.block_rules:
                QMessageBox.warning(self, "Uyarı", "Tanımlı hiçbir IP bloğu kuralı bulunmuyor. Önce kural ekleyin.")
                return

            all_ips = []
            for ips in self.groups.values():
                all_ips.extend(ips)
            all_ips = list(dict.fromkeys(all_ips))

            for g in self.groups:
                self.groups[g] = []

            moved_counts: Dict[str, int] = {}
            for ip in all_ips:
                target_g = match_group_for_ip(ip, self.block_rules, default_group=VARSAYILAN_GRUP)
                self.groups.setdefault(target_g, []).append(ip)
                moved_counts[target_g] = moved_counts.get(target_g, 0) + 1

            self._refresh_group_list()
            summary = "\n".join([f"  • {grp}: {cnt} kamera" for grp, cnt in sorted(moved_counts.items())])
            QMessageBox.information(
                self, "Otomatik Dağıtım Tamamlandı",
                f"Toplam {len(all_ips)} kamera IP blok kurallarına göre dağıtıldı:\n\n{summary}"
            )

        def _refresh_group_list(self) -> None:
            self.group_list.clear()
            for g in sorted(self.groups.keys()):
                count = len(self.groups[g])
                item = QListWidgetItem(f"📁  {g}  ({count} kamera)")
                item.setData(Qt.UserRole, g)
                self.group_list.addItem(item)
            if self.group_list.count() > 0:
                self.group_list.setCurrentRow(0)

        def _refresh_cam_list(self) -> None:
            item = self.group_list.currentItem()
            if not item:
                self.cam_list.clear()
                self.cam_count_lbl.setText("")
                return
            group_name = item.data(Qt.UserRole)
            ips = self.groups.get(group_name, [])
            self.cam_header_lbl.setText(f"KAMERALAR  ➜  {group_name}")
            self.cam_list.clear()
            for ip in sorted(ips):
                self.cam_list.addItem(ip)
            self.cam_count_lbl.setText(f"Bu Grupta Toplam: {len(ips)} kamera")

        def on_group_selected(self, _row: int) -> None:
            self._refresh_cam_list()

        def current_group_name(self) -> Optional[str]:
            item = self.group_list.currentItem()
            return item.data(Qt.UserRole) if item else None

        def add_group(self) -> None:
            name, ok = QInputDialog.getText(
                self, "Yeni İstasyon / Grup Oluştur",
                "İstasyon / Grup Adını Girin (Örn: Milli Kütüphane):",
                QLineEdit.Normal, ""
            )
            if ok and name.strip():
                name = name.strip()
                if name in self.groups:
                    QMessageBox.warning(self, "Uyarı", f"'{name}' grubu zaten mevcut!")
                    return
                self.groups[name] = []
                self._refresh_group_list()
                for i in range(self.group_list.count()):
                    if self.group_list.item(i).data(Qt.UserRole) == name:
                        self.group_list.setCurrentRow(i)
                        break

        def rename_group(self) -> None:
            current = self.current_group_name()
            if not current:
                return
            new_name, ok = QInputDialog.getText(
                self, "İstasyon Adı Değiştir", "Yeni istasyon / grup adını girin:",
                QLineEdit.Normal, current
            )
            if ok and new_name.strip() and new_name.strip() != current:
                new_name = new_name.strip()
                if new_name in self.groups:
                    QMessageBox.warning(self, "Uyarı", f"'{new_name}' grubu zaten mevcut!")
                    return
                self.groups[new_name] = self.groups.pop(current)
                for pat, grp in list(self.block_rules.items()):
                    if grp == current:
                        self.block_rules[pat] = new_name
                self._refresh_group_list()

        def delete_group(self) -> None:
            current = self.current_group_name()
            if not current:
                return
            cam_count = len(self.groups.get(current, []))
            msg = f"'{current}' grubunu silmek istediğinize emin misiniz?"
            if cam_count > 0:
                msg += f"\n\nBu gruptaki {cam_count} kamera '{VARSAYILAN_GRUP}' grubuna aktarılacak."
            reply = QMessageBox.question(self, "Grup Silme Onayı", msg,
                                         QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.Yes:
                ips = self.groups.pop(current, [])
                if ips:
                    self.groups.setdefault(VARSAYILAN_GRUP, [])
                    for ip in ips:
                        if ip not in self.groups[VARSAYILAN_GRUP]:
                            self.groups[VARSAYILAN_GRUP].append(ip)
                self._refresh_group_list()

        def move_to_selected_group(self) -> None:
            src_group = self.current_group_name()
            if not src_group:
                return
            selected_items = self.cam_list.selectedItems()
            if not selected_items:
                QMessageBox.information(self, "Bilgi", "Lütfen taşımak istediğiniz kamera(ları) seçin.")
                return
            other_groups = [g for g in sorted(self.groups.keys()) if g != src_group]
            if not other_groups:
                QMessageBox.information(self, "Bilgi", "Taşıyabileceğiniz başka istasyon/grup yok. Önce yeni bir grup oluşturun.")
                return
            target, ok = QInputDialog.getItem(
                self, "Gruba Taşı",
                f"Seçili {len(selected_items)} kamerayı hangi gruba taşıyalım?",
                other_groups, 0, False
            )
            if not ok:
                return
            ips_to_move = [item.text().strip() for item in selected_items]
            src_list = self.groups.get(src_group, [])
            dst_list = self.groups.setdefault(target, [])
            for ip in ips_to_move:
                if ip in src_list:
                    src_list.remove(ip)
                if ip not in dst_list:
                    dst_list.append(ip)
            self._refresh_group_list()
            for i in range(self.group_list.count()):
                if self.group_list.item(i).data(Qt.UserRole) == src_group:
                    self.group_list.setCurrentRow(i)
                    break
            self._refresh_cam_list()

        def move_back_to_group(self) -> None:
            src_group = self.current_group_name()
            if not src_group or src_group == VARSAYILAN_GRUP:
                return
            selected_items = self.cam_list.selectedItems()
            if not selected_items:
                return
            ips_to_move = [item.text().strip() for item in selected_items]
            src_list = self.groups.get(src_group, [])
            dst_list = self.groups.setdefault(VARSAYILAN_GRUP, [])
            for ip in ips_to_move:
                if ip in src_list:
                    src_list.remove(ip)
                if ip not in dst_list:
                    dst_list.append(ip)
            self._refresh_group_list()
            for i in range(self.group_list.count()):
                if self.group_list.item(i).data(Qt.UserRole) == src_group:
                    self.group_list.setCurrentRow(i)
                    break
            self._refresh_cam_list()


    class MetricCard(QFrame):
        def __init__(self, title: str, value: str = "0", accent_color: str = "#38bdf8", parent=None):
            super().__init__(parent)
            self.setProperty("class", "CardFrame")
            self.setStyleSheet(f"""
                QFrame {{
                    background-color: #1e293b;
                    border: 1px solid #334155;
                    border-left: 4px solid {accent_color};
                    border-radius: 8px;
                }}
            """)
            self.setMinimumHeight(75)
            layout = QVBoxLayout(self)
            layout.setContentsMargins(16, 12, 16, 12)
            layout.setSpacing(4)
            self.title_label = QLabel(title.upper())
            self.title_label.setStyleSheet("color: #94a3b8; font-size: 11px; font-weight: bold;")
            self.val_label = QLabel(value)
            self.val_label.setStyleSheet(f"color: {accent_color}; font-size: 24px; font-weight: bold;")
            layout.addWidget(self.title_label)
            layout.addWidget(self.val_label)

        def set_value(self, val) -> None:
            self.val_label.setText(str(val))


    class PreviewGrabberThread(QThread):
        frame_ready = pyqtSignal(object)
        error_occurred = pyqtSignal(str)

        def __init__(self, ip: str, stream_path: str, target_size: QtCore.QSize):
            super().__init__()
            self.ip = ip
            self.stream_path = stream_path
            self.target_size = target_size
            self._is_cancelled = False

        def run(self):
            rtsp_url = f"rtsp://{self.ip}/{self.stream_path}"
            cap = None
            try:
                cap = cv2.VideoCapture(rtsp_url, cv2.CAP_FFMPEG)
                cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)
                if self._is_cancelled:
                    return
                if not cap.isOpened():
                    self.error_occurred.emit(f"RTSP akışına bağlanılamadı ({rtsp_url})")
                    return
                ret, frame = cap.read()
                if self._is_cancelled:
                    return
                if not ret or frame is None:
                    self.error_occurred.emit("Kamera bağlantısı sağlandı fakat görüntü karesi okunamadı (Sinyal Yok)")
                    return
                rgb_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
                h, w, ch = rgb_frame.shape
                bytes_per_line = ch * w
                q_img = QImage(rgb_frame.data, w, h, bytes_per_line, QImage.Format_RGB888)
                pixmap = QPixmap.fromImage(q_img)
                scaled_pixmap = pixmap.scaled(self.target_size, Qt.KeepAspectRatio, Qt.SmoothTransformation)
                self.frame_ready.emit(scaled_pixmap)
            except Exception as e:
                self.error_occurred.emit(f"Bağlantı hatası: {str(e)}")
            finally:
                if cap is not None:
                    cap.release()

        def cancel(self):
            self._is_cancelled = True


    class CameraPreviewDialog(QDialog):
        def __init__(self, ip: str, config: MonitorConfig, name: str = "", parent=None):
            super().__init__(parent)
            self.ip = ip
            self.config = config
            self.name = name
            self.grabber_thread: Optional[PreviewGrabberThread] = None

            title_text = f"Canlı Önizleme - {name} [{ip}]" if name else f"Canlı Önizleme - [{ip}]"
            self.setWindowTitle(title_text)
            self.setMinimumSize(680, 480)
            self.setStyleSheet(ANKARA_METRO_DARK_STYLE)
            layout = QVBoxLayout(self)
            layout.setContentsMargins(16, 16, 16, 16)
            layout.setSpacing(12)

            header_frame = QFrame()
            header_frame.setProperty("class", "CardFrame")
            h_layout = QHBoxLayout(header_frame)
            h_layout.setContentsMargins(12, 8, 12, 8)
            rtsp_url = f"rtsp://{ip}/{config.rtsp_stream_path}"
            name_badge = f"<span style='color:#a78bfa; font-weight:bold;'>[{name}]</span> " if name else ""
            info_label = QLabel(f"<b>Kamera:</b> {name_badge}{ip} | <b>Akış:</b> <span style='color:#38bdf8;'>{rtsp_url}</span>")
            info_label.setTextFormat(Qt.RichText)
            h_layout.addWidget(info_label)

            self.refresh_btn = QPushButton("🔄 Yenile")
            self.refresh_btn.clicked.connect(self.fetch_preview_frame)
            h_layout.addWidget(self.refresh_btn)
            layout.addWidget(header_frame)

            self.image_label = QLabel("⏳ Kamera akışına bağlanılıyor, lütfen bekleyin...")
            self.image_label.setAlignment(Qt.AlignCenter)
            self.image_label.setStyleSheet("background-color: #020617; border: 2px dashed #334155; border-radius: 8px; color: #94a3b8; font-size: 14px;")
            self.image_label.setMinimumHeight(380)
            layout.addWidget(self.image_label)

            close_btn = QPushButton("Kapat")
            close_btn.clicked.connect(self.close)
            layout.addWidget(close_btn, alignment=Qt.AlignRight)
            QtCore.QTimer.singleShot(50, self.fetch_preview_frame)

        def fetch_preview_frame(self) -> None:
            if cv2 is None:
                self.image_label.setText("❌ Hata: opencv-python (cv2) kurulu değil!")
                return
            if self.grabber_thread and self.grabber_thread.isRunning():
                return
            self.image_label.setText("⏳ Kamera akışına bağlanılıyor, lütfen bekleyin...")
            self.refresh_btn.setEnabled(False)
            target_size = self.image_label.size()
            if target_size.width() <= 0 or target_size.height() <= 0:
                target_size = QtCore.QSize(640, 360)
            self.grabber_thread = PreviewGrabberThread(self.ip, self.config.rtsp_stream_path, target_size)
            self.grabber_thread.frame_ready.connect(self.on_frame_ready)
            self.grabber_thread.error_occurred.connect(self.on_frame_error)
            self.grabber_thread.start()

        @pyqtSlot(object)
        def on_frame_ready(self, pixmap: QPixmap) -> None:
            self.refresh_btn.setEnabled(True)
            if pixmap and not pixmap.isNull():
                self.image_label.setPixmap(pixmap)
            else:
                self.image_label.setText("❌ Geçersiz görüntü karesi alındı.")

        @pyqtSlot(str)
        def on_frame_error(self, err_msg: str) -> None:
            self.refresh_btn.setEnabled(True)
            self.image_label.setText(f"❌ {err_msg}\n({self.ip})")

        def closeEvent(self, event: QtGui.QCloseEvent) -> None:
            if self.grabber_thread and self.grabber_thread.isRunning():
                self.grabber_thread.cancel()
                self.grabber_thread.wait(400)
            super().closeEvent(event)


    class ScanWorker(QThread):
        camera_checked = pyqtSignal(object)
        cycle_finished = pyqtSignal(dict)

        def __init__(self, ip_list: List[str], config: MonitorConfig, states: Dict[str, CameraState]):
            super().__init__()
            self.ip_list = list(ip_list)
            self.config = config
            self.states = states
            self.network_checker = NetworkChecker()
            self.video_analyzer = VideoStreamAnalyzer(config)
            self.is_running = True

        def run(self):
            threads = []

            def worker(ip: str):
                if not self.is_running:
                    return
                ping_ok, ping_ms, ping_err = self.network_checker.ping(ip, timeout_ms=self.config.ping_timeout_ms)
                if not ping_ok:
                    res = CheckResult(ip=ip, is_ping_ok=False, is_stream_ok=False, is_frozen=False,
                                      error_message=ping_err, ping_time_ms=ping_ms)
                else:
                    stream_ok, is_frozen, stream_err = self.video_analyzer.analyze_stream(ip)
                    error_msg = stream_err if not stream_ok or is_frozen else ""
                    res = CheckResult(ip=ip, is_ping_ok=True, is_stream_ok=stream_ok, is_frozen=is_frozen,
                                      error_message=error_msg, ping_time_ms=ping_ms)
                if self.is_running:
                    self.camera_checked.emit(res)

            for ip in self.ip_list:
                t = threading.Thread(target=worker, args=(ip,), daemon=True)
                threads.append(t)
                t.start()
            for t in threads:
                t.join()
            if self.is_running:
                self.cycle_finished.emit(self.states)

        def stop(self):
            self.is_running = False


    class AnkaraMetroMainWindow(QMainWindow):
        COL_IP = 0
        COL_NAME = 1
        COL_GROUP = 2
        COL_STATUS = 3
        COL_PING = 4
        COL_STREAM = 5
        COL_COUNTER = 6
        COL_DESC = 7
        COL_TIME = 8
        COL_ACTION = 9

        def __init__(self):
            super().__init__()
            self.setWindowTitle("Ankara Metro Kamera Kontrol")
            self.resize(1380, 840)
            self.setMinimumSize(1040, 660)
            self.setStyleSheet(ANKARA_METRO_DARK_STYLE)
            self.config = MonitorConfig()
            self.clean_logger = CleanFaultLogger(log_dir=self.config.log_dir)

            self.groups: Dict[str, List[str]] = {}
            self.camera_names: Dict[str, str] = copy.deepcopy(VARSAYILAN_KAMERA_ISIMLERI)
            self.block_rules: Dict[str, str] = copy.deepcopy(VARSAYILAN_BLOK_KURALLARI)
            self.ip_list: List[str] = []
            self.load_camera_data()

            self.states: Dict[str, CameraState] = {}
            for group_name, ips in self.groups.items():
                for ip in ips:
                    cam_name = self.camera_names.get(ip, "")
                    if ip not in self.states:
                        self.states[ip] = CameraState(ip=ip, name=cam_name, group=group_name)

            self.filter_only_faults = False
            self.current_group_filter: str = TUM_GRUPLAR_FILTRE
            self.last_cycle_results: Dict[str, CheckResult] = {}
            self.scan_timer = QtCore.QTimer(self)
            self.scan_timer.timeout.connect(self.trigger_scan)
            self.scan_worker: Optional[ScanWorker] = None
            self.is_monitoring = False

            self.setup_ui()
            self.refresh_group_filter_combo()
            self.update_metrics()
            self.populate_table()

            rule_desc = ", ".join([f"{pat} ➜ {grp}" for pat, grp in self.block_rules.items()])
            named_count = sum(1 for n in self.camera_names.values() if n)
            self.append_clean_log(
                "INFO",
                f"Sistem hazır. Toplam: {len(self.ip_list)} kamera ({named_count} adedi isimlendirilmiş), {len(self.groups)} istasyon grubu."
            )
            self.append_clean_log("INFO", f"⚡ Aktif IP Blok Kuralları: {rule_desc}")

        def load_camera_data(self) -> None:
            if not os.path.exists(KAMERA_AYAR_DOSYASI):
                self._init_defaults()
                return
            try:
                with open(KAMERA_AYAR_DOSYASI, "r", encoding="utf-8") as f:
                    data = json.load(f)

                if isinstance(data, list):
                    ips = list(dict.fromkeys(ip for ip in data if isinstance(ip, str)))
                    self.groups = {VARSAYILAN_GRUP: ips}
                    self.camera_names = copy.deepcopy(VARSAYILAN_KAMERA_ISIMLERI)
                    self.block_rules = copy.deepcopy(VARSAYILAN_BLOK_KURALLARI)
                    self._rebuild_ip_list()
                    self.save_camera_data()
                    return

                if isinstance(data, dict):
                    self.groups = {}
                    raw_groups = data.get("gruplar", {})
                    if isinstance(raw_groups, dict):
                        for g, ips in raw_groups.items():
                            if isinstance(ips, list):
                                self.groups[g] = list(dict.fromkeys(ip for ip in ips if isinstance(ip, str)))

                    grupsuz = data.get("grupsuz", [])
                    if isinstance(grupsuz, list) and grupsuz:
                        existing = {ip for ips in self.groups.values() for ip in ips}
                        new_grupsuz = [ip for ip in grupsuz if isinstance(ip, str) and ip not in existing]
                        if new_grupsuz:
                            self.groups.setdefault(VARSAYILAN_GRUP, []).extend(new_grupsuz)

                    # İsimler
                    names = data.get("kamera_isimleri", {})
                    if isinstance(names, dict):
                        self.camera_names = names
                    else:
                        self.camera_names = copy.deepcopy(VARSAYILAN_KAMERA_ISIMLERI)

                    # Blok kuralları
                    rules = data.get("blok_kurallari", {})
                    if isinstance(rules, dict) and rules:
                        self.block_rules = rules
                    else:
                        self.block_rules = copy.deepcopy(VARSAYILAN_BLOK_KURALLARI)

                    if not self.groups:
                        self._init_defaults()
                    else:
                        self._rebuild_ip_list()
                    return
            except Exception as e:
                print(f"Kamera verisi yüklenemedi: {e}")

            self._init_defaults()

        def _init_defaults(self) -> None:
            self.groups = {
                "Milli Kütüphane": ["172.16.45.55", "172.16.45.56"],
                "Ümitköy": ["172.16.47.55", "172.16.47.56"],
                VARSAYILAN_GRUP: ["192.168.1.101", "192.168.1.102"],
            }
            self.camera_names = copy.deepcopy(VARSAYILAN_KAMERA_ISIMLERI)
            self.block_rules = copy.deepcopy(VARSAYILAN_BLOK_KURALLARI)
            self._rebuild_ip_list()

        def save_camera_data(self) -> None:
            try:
                grupsuz = self.groups.get(VARSAYILAN_GRUP, [])
                named_groups = {k: v for k, v in self.groups.items() if k != VARSAYILAN_GRUP}
                data = {
                    "version": 2,
                    "gruplar": named_groups,
                    "grupsuz": grupsuz,
                    "kamera_isimleri": self.camera_names,
                    "blok_kurallari": self.block_rules,
                }
                with open(KAMERA_AYAR_DOSYASI, "w", encoding="utf-8") as f:
                    json.dump(data, f, ensure_ascii=False, indent=2)
            except Exception as e:
                self.append_clean_log("ERROR", f"Kamera verisi kaydedilemedi: {e}")

        def _rebuild_ip_list(self) -> None:
            seen: set = set()
            result = []
            for ips in self.groups.values():
                for ip in ips:
                    if ip not in seen:
                        seen.add(ip)
                        result.append(ip)
            self.ip_list = result

        def get_all_group_names(self) -> List[str]:
            return sorted(self.groups.keys())

        def get_active_scan_ips(self) -> List[str]:
            """O anda filtrede seçili olan gruba göre taranacak kamera IP listesini döner."""
            if self.current_group_filter == TUM_GRUPLAR_FILTRE:
                return list(self.ip_list)
            grp = self.current_group_filter.replace("📁 ", "").strip()
            return list(self.groups.get(grp, []))

        def update_scan_button_text(self) -> None:
            """Seçili gruba göre tarama butonunun metnini dinamik günceller."""
            if hasattr(self, "scan_now_btn"):
                if self.current_group_filter == TUM_GRUPLAR_FILTRE:
                    self.scan_now_btn.setText("🔄 Şimdi Tara (Tümü)")
                    self.scan_now_btn.setToolTip(f"Tüm istasyonlardaki {len(self.ip_list)} kamerayı tarar")
                else:
                    grp = self.current_group_filter.replace("📁 ", "").strip()
                    cams = self.groups.get(grp, [])
                    self.scan_now_btn.setText(f"🔄 Tara: {grp}")
                    self.scan_now_btn.setToolTip(f"Sadece '{grp}' istasyonundaki {len(cams)} kamerayı tarar")

        def refresh_group_filter_combo(self) -> None:
            self.group_filter_combo.blockSignals(True)
            self.group_filter_combo.clear()
            self.group_filter_combo.addItem(TUM_GRUPLAR_FILTRE)
            for g in self.get_all_group_names():
                self.group_filter_combo.addItem(f"📁 {g}")
            idx = self.group_filter_combo.findText(self.current_group_filter)
            if idx >= 0:
                self.group_filter_combo.setCurrentIndex(idx)
            else:
                self.group_filter_combo.setCurrentIndex(0)
                self.current_group_filter = TUM_GRUPLAR_FILTRE
            self.group_filter_combo.blockSignals(False)
            self.update_scan_button_text()

        def on_group_filter_changed(self, text: str) -> None:
            self.current_group_filter = text
            self.update_scan_button_text()
            self.populate_table()

        def rename_camera_dialog(self, ip: str) -> None:
            """Kameranın adını / konumunu belirleme diyaloğu."""
            curr_name = self.camera_names.get(ip, "")
            new_name, ok = QInputDialog.getText(
                self, "Kamera Adı / Konumu Düzenle",
                f"[{ip}] IP adresli kamera için isim / konum girin:\n(Örn: Turnike Giriş 1, Peron Doğu, Gişe vb.)",
                QLineEdit.Normal, curr_name
            )
            if ok:
                new_name = new_name.strip()
                self.camera_names[ip] = new_name
                if ip in self.states:
                    self.states[ip].name = new_name
                self.save_camera_data()
                self.populate_table()
                display = f"'{new_name}'" if new_name else "(İsimsiz)"
                self.append_clean_log("INFO", f"Kamera adı güncellendi: {ip} ➜ {display}")

        def open_group_manager_dialog(self) -> None:
            dialog_groups: Dict[str, List[str]] = copy.deepcopy(self.groups)
            dialog_rules: Dict[str, str] = copy.deepcopy(self.block_rules)
            dialog = GroupManagerDialog(dialog_groups, dialog_rules, self)
            if dialog.exec_() == QDialog.Accepted:
                self.groups = dialog.groups
                self.block_rules = dialog.block_rules
                for g, ips in self.groups.items():
                    for ip in ips:
                        cam_name = self.camera_names.get(ip, "")
                        if ip in self.states:
                            self.states[ip].group = g
                            self.states[ip].name = cam_name
                        else:
                            self.states[ip] = CameraState(ip=ip, name=cam_name, group=g)
                self._rebuild_ip_list()
                self.save_camera_data()
                self.refresh_group_filter_combo()
                self.populate_table()
                self.update_metrics()
                total_cams = sum(len(v) for v in self.groups.values())
                self.append_clean_log(
                    "SUCCESS",
                    f"Grup ve kural yapılandırması kaydedildi. {len(self.groups)} istasyon/grup, {total_cams} kamera."
                )

        def _quick_move_to_group(self, ips: List[str], target_group: str) -> None:
            for ip in ips:
                for g, gips in self.groups.items():
                    if ip in gips:
                        gips.remove(ip)
                self.groups.setdefault(target_group, [])
                if ip not in self.groups[target_group]:
                    self.groups[target_group].append(ip)
                if ip in self.states:
                    self.states[ip].group = target_group
            self.save_camera_data()
            self.refresh_group_filter_combo()
            self.populate_table()
            self.append_clean_log("INFO", f"{len(ips)} kamera '{target_group}' grubuna taşındı.")

        def setup_ui(self) -> None:
            central_widget = QWidget()
            self.setCentralWidget(central_widget)
            main_layout = QVBoxLayout(central_widget)
            main_layout.setContentsMargins(18, 18, 18, 18)
            main_layout.setSpacing(14)

            # Header
            header_layout = QHBoxLayout()
            title_box = QVBoxLayout()
            main_title = QLabel("ANKARA METRO KAMERA KONTROL")
            main_title.setStyleSheet("font-size: 19px; font-weight: bold; color: #f8fafc; letter-spacing: 0.5px;")
            sub_title = QLabel("Pelco Sarix IP Kamera Sağlık ve Donma Takip Sistemi | İzole Yerel Ağ (Offline LAN)")
            sub_title.setStyleSheet("font-size: 12px; color: #94a3b8;")
            title_box.addWidget(main_title)
            title_box.addWidget(sub_title)
            header_layout.addLayout(title_box)
            header_layout.addStretch()

            self.system_status_badge = QLabel("● SİSTEM BEKLEMEDE")
            self.system_status_badge.setStyleSheet("background-color: #334155; color: #94a3b8; font-weight: bold; font-size: 12px; padding: 6px 14px; border-radius: 12px;")
            header_layout.addWidget(self.system_status_badge)
            main_layout.addLayout(header_layout)

            # Metrics
            metrics_layout = QHBoxLayout()
            metrics_layout.setSpacing(12)
            self.card_total = MetricCard("Toplam Kamera", "0", "#38bdf8")
            self.card_groups = MetricCard("İstasyon / Grup", "0", "#8b5cf6")
            self.card_online = MetricCard("Çevrimiçi (Aktif)", "0", "#22c55e")
            self.card_warning = MetricCard("Uyarı / Dalgalanma", "0", "#f59e0b")
            self.card_faulty = MetricCard("Arızalı / Donmuş", "0", "#ef4444")
            for card in (self.card_total, self.card_groups, self.card_online, self.card_warning, self.card_faulty):
                metrics_layout.addWidget(card)
            main_layout.addLayout(metrics_layout)

            # Controls
            control_frame = QFrame()
            control_frame.setProperty("class", "CardFrame")
            ctrl_layout = QHBoxLayout(control_frame)
            ctrl_layout.setContentsMargins(12, 10, 12, 10)
            ctrl_layout.setSpacing(10)

            self.start_btn = QPushButton("▶ İzlemeyi Başlat")
            self.start_btn.setProperty("class", "SuccessBtn")
            self.start_btn.clicked.connect(self.toggle_monitoring)
            ctrl_layout.addWidget(self.start_btn)

            self.scan_now_btn = QPushButton("🔄 Şimdi Tara (Tümü)")
            self.scan_now_btn.setProperty("class", "PrimaryBtn")
            self.scan_now_btn.clicked.connect(self.trigger_scan)
            ctrl_layout.addWidget(self.scan_now_btn)

            ctrl_layout.addSpacing(6)

            bulk_import_btn = QPushButton("📂 Toplu IP/İsim İçe Aktar")
            bulk_import_btn.setProperty("class", "WarningBtn")
            bulk_import_btn.setToolTip("TXT/CSV'den toplu IP ve kamera adı ekleyin.")
            bulk_import_btn.clicked.connect(self.open_bulk_import_dialog)
            ctrl_layout.addWidget(bulk_import_btn)

            add_btn = QPushButton("➕ Tek Ekle")
            add_btn.clicked.connect(self.add_single_camera_dialog)
            ctrl_layout.addWidget(add_btn)

            group_mgr_btn = QPushButton("📁 İstasyon / Grup Yönetimi")
            group_mgr_btn.setProperty("class", "GroupBtn")
            group_mgr_btn.setToolTip("Kamera gruplarını ve IP blok eşleştirme kurallarını yönetin")
            group_mgr_btn.clicked.connect(self.open_group_manager_dialog)
            ctrl_layout.addWidget(group_mgr_btn)

            excel_btn = QPushButton("📊 Arıza Raporu (Excel)")
            excel_btn.setProperty("class", "ExcelBtn")
            excel_btn.setToolTip("Arızalı kameraları renkli ve biçimlendirilmiş Excel (.xls) veya CSV olarak dışa aktarır.")
            excel_btn.clicked.connect(lambda: self.export_excel_report(only_faults=True))
            ctrl_layout.addWidget(excel_btn)

            del_btn = QPushButton("🗑️ Seçilenleri Sil")
            del_btn.setProperty("class", "DangerBtn")
            del_btn.clicked.connect(self.remove_selected_cameras)
            ctrl_layout.addWidget(del_btn)

            clear_all_btn = QPushButton("⚠️ Tümünü Temizle")
            clear_all_btn.clicked.connect(self.clear_all_cameras)
            ctrl_layout.addWidget(clear_all_btn)

            ctrl_layout.addStretch()

            interval_label = QLabel("Döngü:")
            interval_label.setStyleSheet("color: #94a3b8; font-weight: bold;")
            ctrl_layout.addWidget(interval_label)

            self.interval_spin = QSpinBox()
            self.interval_spin.setRange(5, 600)
            self.interval_spin.setValue(self.config.check_interval_sec)
            self.interval_spin.setSuffix(" sn")
            self.interval_spin.valueChanged.connect(self.update_interval)
            ctrl_layout.addWidget(self.interval_spin)

            main_layout.addWidget(control_frame)

            # Table Container
            table_container = QWidget()
            table_layout = QVBoxLayout(table_container)
            table_layout.setContentsMargins(0, 0, 0, 0)
            table_layout.setSpacing(6)

            table_top_bar = QHBoxLayout()
            table_header = QLabel("CANLI KAMERA İZLEME TABLOSU")
            table_header.setStyleSheet("color: #94a3b8; font-size: 11px; font-weight: bold; letter-spacing: 0.5px;")
            table_top_bar.addWidget(table_header)
            table_top_bar.addSpacing(16)

            filter_label = QLabel("Durum:")
            filter_label.setStyleSheet("color: #94a3b8; font-size: 11px; font-weight: bold;")
            table_top_bar.addWidget(filter_label)

            self.rb_all = QRadioButton("Tümü")
            self.rb_all.setChecked(True)
            self.rb_all.toggled.connect(self.on_filter_changed)
            table_top_bar.addWidget(self.rb_all)

            self.rb_faults = QRadioButton("🚨 Arızalılar / Uyarılar")
            self.rb_faults.toggled.connect(self.on_filter_changed)
            table_top_bar.addWidget(self.rb_faults)

            table_top_bar.addSpacing(16)

            grp_filter_lbl = QLabel("İstasyon / Grup:")
            grp_filter_lbl.setStyleSheet("color: #94a3b8; font-size: 11px; font-weight: bold;")
            table_top_bar.addWidget(grp_filter_lbl)

            self.group_filter_combo = QComboBox()
            self.group_filter_combo.setMinimumWidth(180)
            self.group_filter_combo.currentTextChanged.connect(self.on_group_filter_changed)
            table_top_bar.addWidget(self.group_filter_combo)

            table_top_bar.addStretch()

            hint_lbl = QLabel("💡 <i>Kamera adına çift tıklayarak değiştirebilirsiniz</i>")
            hint_lbl.setStyleSheet("color: #64748b; font-size: 11px;")
            table_top_bar.addWidget(hint_lbl)
            table_top_bar.addSpacing(10)

            export_excel_all_btn = QPushButton("📊 Excel Raporu Al")
            export_excel_all_btn.setProperty("class", "ExcelBtn")
            export_excel_all_btn.clicked.connect(self.prompt_export_excel)
            table_top_bar.addWidget(export_excel_all_btn)

            export_txt_btn = QPushButton("💾 TXT Dışa Aktar")
            export_txt_btn.clicked.connect(self.export_ip_list)
            table_top_bar.addWidget(export_txt_btn)

            table_layout.addLayout(table_top_bar)

            self.table = QTableWidget()
            self.table.setColumnCount(10)
            self.table.setHorizontalHeaderLabels([
                "Kamera IP", "Kamera Adı / Konum", "İstasyon / Grup", "Durum", "Ping (ms)", "RTSP Akış",
                "Hata Sayacı", "Arıza / Durum Tanısı", "Son Kontrol", "İşlem"
            ])
            self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
            self.table.horizontalHeader().setStretchLastSection(False)
            self.table.setColumnWidth(self.COL_IP, 125)
            self.table.setColumnWidth(self.COL_NAME, 160)
            self.table.setColumnWidth(self.COL_GROUP, 140)
            self.table.setColumnWidth(self.COL_STATUS, 95)
            self.table.setColumnWidth(self.COL_PING, 80)
            self.table.setColumnWidth(self.COL_STREAM, 100)
            self.table.setColumnWidth(self.COL_COUNTER, 85)
            self.table.setColumnWidth(self.COL_DESC, 230)
            self.table.setColumnWidth(self.COL_TIME, 95)
            self.table.setColumnWidth(self.COL_ACTION, 115)
            self.table.setSelectionBehavior(QTableWidget.SelectRows)
            self.table.setSelectionMode(QTableWidget.ExtendedSelection)
            self.table.setContextMenuPolicy(Qt.CustomContextMenu)
            self.table.customContextMenuRequested.connect(self.show_table_context_menu)
            self.table.cellDoubleClicked.connect(self.on_table_cell_double_clicked)
            self.table.setEditTriggers(QTableWidget.NoEditTriggers)
            table_layout.addWidget(self.table)

            # Log console
            log_container = QWidget()
            log_layout = QVBoxLayout(log_container)
            log_layout.setContentsMargins(0, 4, 0, 0)
            log_layout.setSpacing(6)
            log_top_layout = QHBoxLayout()
            log_header = QLabel("TEMİZ ARIZA VE DURUM GÜNLÜĞÜ (Sadece Kritik Olaylar)")
            log_header.setStyleSheet("color: #94a3b8; font-size: 11px; font-weight: bold; letter-spacing: 0.5px;")
            log_top_layout.addWidget(log_header)
            log_top_layout.addStretch()

            open_summary_btn = QPushButton("📑 Güncel Arıza Özetini Aç")
            open_summary_btn.clicked.connect(self.open_active_faults_summary)
            log_top_layout.addWidget(open_summary_btn)

            open_daily_log_btn = QPushButton("📁 Günlük Arıza Logunu Aç")
            open_daily_log_btn.clicked.connect(self.open_daily_log_file)
            log_top_layout.addWidget(open_daily_log_btn)

            clear_log_btn = QPushButton("🧹 Temizle")
            clear_log_btn.clicked.connect(lambda: self.log_console.clear())
            log_top_layout.addWidget(clear_log_btn)

            log_layout.addLayout(log_top_layout)

            self.log_console = QTextEdit()
            self.log_console.setReadOnly(True)
            log_layout.addWidget(self.log_console)

            splitter = QSplitter(Qt.Vertical)
            splitter.setStyleSheet("QSplitter::handle { background-color: #334155; height: 4px; }")
            splitter.addWidget(table_container)
            splitter.addWidget(log_container)
            splitter.setSizes([460, 180])
            main_layout.addWidget(splitter)

        def on_table_cell_double_clicked(self, row: int, col: int) -> None:
            """Hücreye çift tıklandığında (IP veya İsim sütunu) kamera adı düzenleme diyaloğunu açar."""
            if col in (self.COL_IP, self.COL_NAME):
                visible_ips = self.get_filtered_ips()
                if 0 <= row < len(visible_ips):
                    self.rename_camera_dialog(visible_ips[row])

        def populate_table(self) -> None:
            visible_ips = self.get_filtered_ips()
            self.table.setRowCount(len(visible_ips))
            for row, ip in enumerate(visible_ips):
                st = self.states.get(ip, CameraState(ip=ip, name=self.camera_names.get(ip, "")))

                # IP
                ip_item = QTableWidgetItem(ip)
                ip_item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(row, self.COL_IP, ip_item)

                # Kamera Adı / Konum
                cam_name = st.name if st.name else "-"
                name_item = QTableWidgetItem(cam_name)
                name_item.setTextAlignment(Qt.AlignCenter)
                name_item.setToolTip(f"Çift tıklayarak {ip} kamerasının adını değiştirebilirsiniz")
                if st.name:
                    name_item.setForeground(QColor("#38bdf8"))
                    name_item.setFont(QtGui.QFont("Segoe UI", 9, QtGui.QFont.Bold))
                else:
                    name_item.setForeground(QColor("#64748b"))
                self.table.setItem(row, self.COL_NAME, name_item)

                # Grup
                group_item = QTableWidgetItem(st.group)
                group_item.setTextAlignment(Qt.AlignCenter)
                group_item.setForeground(QColor("#a78bfa"))
                self.table.setItem(row, self.COL_GROUP, group_item)

                # Durum
                self.update_row_status(row, st)

                # Ping
                ping_item = QTableWidgetItem("-")
                ping_item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(row, self.COL_PING, ping_item)

                # RTSP
                stream_item = QTableWidgetItem("Beklemede")
                stream_item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(row, self.COL_STREAM, stream_item)

                # Sayaç
                counter_item = QTableWidgetItem(f"{st.consecutive_failures} / {self.config.consecutive_fail_limit}")
                counter_item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(row, self.COL_COUNTER, counter_item)

                # Açıklama
                desc_item = QTableWidgetItem(st.last_error or "Hazır (Henüz taranmadı)")
                self.table.setItem(row, self.COL_DESC, desc_item)

                # Zaman
                time_str = st.last_check_time.strftime("%H:%M:%S") if st.last_check_time else "-"
                time_item = QTableWidgetItem(time_str)
                time_item.setTextAlignment(Qt.AlignCenter)
                self.table.setItem(row, self.COL_TIME, time_item)

                # İşlem Butonları
                action_widget = QWidget()
                action_layout = QHBoxLayout(action_widget)
                action_layout.setContentsMargins(2, 2, 2, 2)
                action_layout.setSpacing(4)

                preview_btn = QPushButton("👁")
                preview_btn.setToolTip(f"{ip} Canlı Önizle")
                preview_btn.setStyleSheet("QPushButton { background-color: #0f172a; border: 1px solid #38bdf8; color: #38bdf8; padding: 3px 6px; font-size: 11px; } QPushButton:hover { background-color: #38bdf8; color: #0f172a; }")
                preview_btn.clicked.connect(lambda checked, tgt=ip: self.show_preview_dialog(tgt))
                action_layout.addWidget(preview_btn)

                edit_name_btn = QPushButton("✏️")
                edit_name_btn.setToolTip(f"{ip} Kamera Adını Düzenle")
                edit_name_btn.setStyleSheet("QPushButton { background-color: #0f172a; border: 1px solid #a78bfa; color: #a78bfa; padding: 3px 6px; font-size: 11px; } QPushButton:hover { background-color: #a78bfa; color: #0f172a; }")
                edit_name_btn.clicked.connect(lambda checked, tgt=ip: self.rename_camera_dialog(tgt))
                action_layout.addWidget(edit_name_btn)

                del_row_btn = QPushButton("🗑")
                del_row_btn.setToolTip(f"{ip} Kamerasını Sil")
                del_row_btn.setStyleSheet("QPushButton { background-color: #0f172a; border: 1px solid #ef4444; color: #ef4444; padding: 3px 6px; font-size: 11px; } QPushButton:hover { background-color: #ef4444; color: #fff; }")
                del_row_btn.clicked.connect(lambda checked, tgt=ip: self.remove_single_camera_by_ip(tgt))
                action_layout.addWidget(del_row_btn)

                self.table.setCellWidget(row, self.COL_ACTION, action_widget)

        def get_filtered_ips(self) -> List[str]:
            is_all = self.current_group_filter == TUM_GRUPLAR_FILTRE
            if is_all:
                base_ips = self.ip_list
            else:
                g = self.current_group_filter.replace("📁 ", "").strip()
                base_ips = self.groups.get(g, [])
            if not self.filter_only_faults:
                return [ip for ip in base_ips if ip in self.states or ip in self.ip_list]
            return [ip for ip in base_ips if self.states.get(ip) and self.states[ip].status in ("ARIZALI", "UYARI")]

        def on_filter_changed(self) -> None:
            self.filter_only_faults = self.rb_faults.isChecked()
            self.populate_table()

        def update_row_status(self, row: int, state: CameraState) -> None:
            status_label = QLabel()
            status_label.setAlignment(Qt.AlignCenter)
            if state.status == "ONLINE":
                status_label.setText("ONLINE")
                status_label.setStyleSheet("background-color: #14532d; color: #4ade80; font-weight: bold; font-size: 11px; border-radius: 4px; padding: 4px;")
            elif state.status == "UYARI":
                status_label.setText("UYARI")
                status_label.setStyleSheet("background-color: #78350f; color: #fde047; font-weight: bold; font-size: 11px; border-radius: 4px; padding: 4px;")
            else:
                status_label.setText("ARIZALI")
                status_label.setStyleSheet("background-color: #7f1d1d; color: #f87171; font-weight: bold; font-size: 11px; border-radius: 4px; padding: 4px;")
            self.table.setCellWidget(row, self.COL_STATUS, status_label)

        def update_metrics(self) -> None:
            total = len(self.ip_list)
            groups_count = len(self.groups)
            online = sum(1 for s in self.states.values() if s.status == "ONLINE")
            warning = sum(1 for s in self.states.values() if s.status == "UYARI")
            faulty = sum(1 for s in self.states.values() if s.status == "ARIZALI")
            self.card_total.set_value(total)
            self.card_groups.set_value(groups_count)
            self.card_online.set_value(online)
            self.card_warning.set_value(warning)
            self.card_faulty.set_value(faulty)

        def append_clean_log(self, level: str, message: str) -> None:
            timestamp = datetime.now().strftime("%H:%M:%S")
            color_map = {"INFO": "#38bdf8", "WARNING": "#f59e0b", "ERROR": "#ef4444", "CRITICAL": "#f87171", "SUCCESS": "#22c55e"}
            color = color_map.get(level, "#e2e8f0")
            html = (f"<span style='color:#64748b;'>[{timestamp}]</span> "
                    f"<span style='color:{color}; font-weight:bold;'>[{level}]</span> "
                    f"<span style='color:#f8fafc;'>{message}</span>")
            self.log_console.append(html)

        # ------------------------------------------------------------------
        # EXCEL RAPOR DIŞA AKTARMA METODLARI
        # ------------------------------------------------------------------
        def prompt_export_excel(self) -> None:
            faulty_count = sum(1 for s in self.states.values() if s.status in ("ARIZALI", "UYARI"))
            if faulty_count == 0:
                reply = QMessageBox.question(
                    self, "Excel Raporu",
                    "Şu anda sistemde hiçbir arızalı veya uyarıda kamera bulunmuyor.\n\n"
                    "Tüm kameraların sağlık durum raporunu Excel olarak kaydetmek ister misiniz?",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.Yes
                )
                if reply == QMessageBox.Yes:
                    self.export_excel_report(only_faults=False)
                return

            msg_box = QMessageBox(self)
            msg_box.setWindowTitle("Excel Raporu Seçimi")
            msg_box.setText("Hangi kameraları Excel raporuna aktarmak istersiniz?")
            btn_faults = msg_box.addButton(f"🚨 Sadece Arızalı / Uyarıdakiler ({faulty_count} adet)", QMessageBox.ActionRole)
            btn_all = msg_box.addButton(f"📋 Tüm Kameralar ({len(self.ip_list)} adet)", QMessageBox.ActionRole)
            btn_cancel = msg_box.addButton("İptal", QMessageBox.RejectRole)
            msg_box.exec_()

            if msg_box.clickedButton() == btn_faults:
                self.export_excel_report(only_faults=True)
            elif msg_box.clickedButton() == btn_all:
                self.export_excel_report(only_faults=False)

        def export_excel_report(self, only_faults: bool = True) -> None:
            faulty_cams = [s for s in self.states.values() if s.status in ("ARIZALI", "UYARI")]
            if only_faults and not faulty_cams:
                QMessageBox.information(
                    self, "Bilgi",
                    "Şu anda sistemde hiçbir arızalı veya uyarıda kamera bulunmuyor!\n"
                    "Tüm sistem aktif ve sağlıklı çalışıyor."
                )
                return

            date_str = datetime.now().strftime("%Y_%m_%d_%H%M")
            default_filename = f"ankara_metro_arizali_kameralar_{date_str}.xls" if only_faults else f"ankara_metro_tum_kameralar_{date_str}.xls"

            file_path, selected_filter = QFileDialog.getSaveFileName(
                self,
                "Excel Arıza Raporunu Kaydet",
                default_filename,
                "Excel Çalışma Sayfası (*.xls);;Excel Uyumlu CSV (*.csv);;Tüm Dosyalar (*.*)"
            )

            if not file_path:
                return

            try:
                if file_path.lower().endswith(".csv"):
                    count = ExcelReportExporter.export_to_csv(
                        file_path, self.states, self.last_cycle_results, only_faults=only_faults
                    )
                else:
                    if not file_path.lower().endswith(".xls"):
                        file_path += ".xls"
                    count = ExcelReportExporter.export_to_excel_xml(
                        file_path, self.states, self.last_cycle_results, only_faults=only_faults
                    )

                self.append_clean_log(
                    "SUCCESS",
                    f"📊 Excel raporu başarıyla kaydedildi: {os.path.basename(file_path)} ({count} kamera)"
                )

                reply = QMessageBox.question(
                    self,
                    "Rapor Başarıyla Oluşturuldu",
                    f"Toplam {count} kamerayı içeren Excel raporu kaydedildi:\n{file_path}\n\n"
                    "Raporu şimdi Microsoft Excel ile açmak ister misiniz?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.Yes
                )
                if reply == QMessageBox.Yes:
                    if sys.platform == "win32":
                        os.startfile(file_path)
                    else:
                        subprocess.Popen(["xdg-open", file_path])

            except Exception as ex:
                QMessageBox.critical(self, "Hata", f"Excel raporu oluşturulamadı: {ex}")
                self.append_clean_log("ERROR", f"Excel export hatası: {ex}")

        def open_active_faults_summary(self) -> None:
            path = os.path.abspath(os.path.join(self.config.log_dir, GUNCEL_ARIZA_DOSYASI))
            self.clean_logger.update_active_faults_summary(self.states)
            if os.path.exists(path):
                if sys.platform == "win32":
                    os.startfile(path)
                else:
                    subprocess.Popen(["xdg-open", path])
            else:
                QMessageBox.information(self, "Bilgi", "Henüz bir arıza özeti oluşmadı.")

        def open_daily_log_file(self) -> None:
            date_str = datetime.now().strftime("%Y_%m_%d")
            path = os.path.abspath(os.path.join(self.config.log_dir, f"kamera_ariza_{date_str}.log"))
            if not os.path.exists(path):
                path = os.path.abspath(self.config.log_dir)
            if os.path.exists(path):
                if sys.platform == "win32":
                    os.startfile(path)
                else:
                    subprocess.Popen(["xdg-open", path])
            else:
                QMessageBox.information(self, "Bilgi", "Henüz kaydedilmiş bir arıza kaydı bulunmuyor.")

        def update_interval(self, value: int) -> None:
            self.config.check_interval_sec = value
            if self.is_monitoring:
                self.scan_timer.setInterval(value * 1000)

        def toggle_monitoring(self) -> None:
            if not self.is_monitoring:
                self.is_monitoring = True
                self.start_btn.setText("⏹ İzlemeyi Durdur")
                self.start_btn.setProperty("class", "DangerBtn")
                self.start_btn.setStyle(self.start_btn.style())
                self.system_status_badge.setText("● AKTİF İZLEME DEVREDE")
                self.system_status_badge.setStyleSheet("background-color: #14532d; color: #4ade80; font-weight: bold; font-size: 12px; padding: 6px 14px; border-radius: 12px;")
                self.append_clean_log("SUCCESS", f"Kamera izleme başlatıldı (Döngü: {self.config.check_interval_sec} sn).")
                self.trigger_scan()
                self.scan_timer.start(self.config.check_interval_sec * 1000)
            else:
                self.is_monitoring = False
                self.scan_timer.stop()
                self.start_btn.setText("▶ İzlemeyi Başlat")
                self.start_btn.setProperty("class", "SuccessBtn")
                self.start_btn.setStyle(self.start_btn.style())
                self.system_status_badge.setText("● SİSTEM DURDURULDU")
                self.system_status_badge.setStyleSheet("background-color: #334155; color: #94a3b8; font-weight: bold; font-size: 12px; padding: 6px 14px; border-radius: 12px;")
                self.append_clean_log("WARNING", "Kamera izleme kullanıcı tarafından durduruldu.")

        def trigger_scan(self) -> None:
            if self.scan_worker and self.scan_worker.isRunning():
                return

            target_ips = self.get_active_scan_ips()
            if not target_ips:
                self.append_clean_log("WARNING", "Seçili istasyon/grupta taranacak kamera bulunamadı.")
                return

            grp_name = "Tüm Kameralar" if self.current_group_filter == TUM_GRUPLAR_FILTRE else self.current_group_filter.replace("📁 ", "").strip()
            self.append_clean_log("INFO", f"🔄 Tarama başlatıldı: [{grp_name}] ({len(target_ips)} kamera taranıyor)...")

            self.scan_now_btn.setEnabled(False)
            self.scan_worker = ScanWorker(target_ips, self.config, self.states)
            self.scan_worker.camera_checked.connect(self.on_camera_checked)
            self.scan_worker.cycle_finished.connect(
                lambda states_dict, t_ips=target_ips, g_name=grp_name: self.on_cycle_finished(states_dict, t_ips, g_name)
            )
            self.scan_worker.start()

        @pyqtSlot(object)
        def on_camera_checked(self, result: CheckResult) -> None:
            ip = result.ip
            if ip not in self.states:
                return
            state = self.states[ip]
            state.last_check_time = datetime.now()
            self.last_cycle_results[ip] = result
            name_display = f"{state.name} ({ip})" if state.name else ip

            if result.is_healthy:
                if state.status == "ARIZALI":
                    self.clean_logger.log_recovery(ip, result.ping_time_ms, group=state.group, name=state.name)
                    self.append_clean_log("SUCCESS", f"✅ [KURTARILDI] [{state.group}] {name_display} normale döndü! (Ping: {result.ping_time_ms:.1f}ms)")
                elif state.consecutive_failures > 0:
                    self.append_clean_log("INFO", f"ℹ️  [{state.group}] {name_display} geçici aksaklıktan kurtuldu.")
                state.consecutive_failures = 0
                state.status = "ONLINE"
                state.last_error = ""
                state.first_failure_time = None
            else:
                state.consecutive_failures += 1
                state.last_error = result.error_message
                if state.first_failure_time is None:
                    state.first_failure_time = datetime.now()
                if state.consecutive_failures >= self.config.consecutive_fail_limit:
                    is_new_fault = (state.status != "ARIZALI")
                    state.status = "ARIZALI"
                    if is_new_fault:
                        self.clean_logger.log_fault(ip=ip, error_msg=result.error_message,
                                                     fail_count=state.consecutive_failures,
                                                     first_time=state.first_failure_time, group=state.group,
                                                     name=state.name)
                        self.append_clean_log("CRITICAL",
                            f"🚨 [ARIZALI] [{state.group}] {name_display} üst üste {state.consecutive_failures} kez başarısız! Hata: {result.error_message}")
                else:
                    state.status = "UYARI"
                    self.append_clean_log("WARNING",
                        f"⚠️ [UYARI] [{state.group}] {name_display} ({state.consecutive_failures}/{self.config.consecutive_fail_limit}). {result.error_message}")

            visible_ips = self.get_filtered_ips()
            if ip in visible_ips:
                row = visible_ips.index(ip)
                ping_str = f"{result.ping_time_ms:.1f} ms" if result.is_ping_ok else "Zaman Aşımı"
                self.table.item(row, self.COL_PING).setText(ping_str)
                if not result.is_ping_ok:
                    stream_str = "Erişilemez"
                elif result.is_frozen:
                    stream_str = "DONDU (Frozen)"
                elif result.is_stream_ok:
                    stream_str = "Aktif (Canlı)"
                else:
                    stream_str = "Kesildi"
                self.table.item(row, self.COL_STREAM).setText(stream_str)
                self.table.item(row, self.COL_COUNTER).setText(f"{state.consecutive_failures} / {self.config.consecutive_fail_limit}")
                self.table.item(row, self.COL_DESC).setText(state.last_error if state.last_error else "Sağlıklı (Sorun yok)")
                self.table.item(row, self.COL_TIME).setText(state.last_check_time.strftime("%H:%M:%S"))
                self.update_row_status(row, state)
            self.update_metrics()

        def on_cycle_finished(self, _states: dict, target_ips: Optional[List[str]] = None, group_name: str = "Tüm Kameralar") -> None:
            self.scan_now_btn.setEnabled(True)
            self.clean_logger.log_scan_cycle(self.states, self.last_cycle_results)
            self.clean_logger.update_active_faults_summary(self.states)
            self.update_metrics()
            if self.filter_only_faults:
                self.populate_table()

            if target_ips:
                scoped_states = [self.states[ip] for ip in target_ips if ip in self.states]
            else:
                scoped_states = list(self.states.values())

            total = len(scoped_states)
            online = sum(1 for s in scoped_states if s.status == "ONLINE")
            warning = sum(1 for s in scoped_states if s.status == "UYARI")
            faulty = sum(1 for s in scoped_states if s.status == "ARIZALI")
            cycle_time = datetime.now().strftime("%H:%M:%S")
            cycle_summary = f"[{group_name}] Taraması Tamamlandı ({cycle_time}) -> Toplam: {total} | Çevrimiçi: {online} | Uyarı: {warning} | Arızalı: {faulty}"
            if faulty > 0:
                self.append_clean_log("CRITICAL", f"🚨 {cycle_summary}")
            elif warning > 0:
                self.append_clean_log("WARNING", f"⚠️ {cycle_summary}")
            else:
                self.append_clean_log("SUCCESS", f"✅ {cycle_summary}")

        def show_preview_dialog(self, ip: str) -> None:
            cam_name = self.camera_names.get(ip, "")
            dialog = CameraPreviewDialog(ip, self.config, name=cam_name, parent=self)
            dialog.exec_()

        def open_bulk_import_dialog(self) -> None:
            dialog = BulkImportDialog(self.ip_list, self.get_all_group_names(), self.block_rules, self)
            if dialog.exec_() == QDialog.Accepted and dialog.imported_pairs:
                for ip, name in dialog.imported_pairs:
                    if name:
                        self.camera_names[ip] = name
                    target_group = dialog.ip_to_group_map.get(ip, VARSAYILAN_GRUP)
                    self.groups.setdefault(target_group, [])
                    if ip not in self.groups[target_group]:
                        self.groups[target_group].append(ip)
                    cam_name = self.camera_names.get(ip, "")
                    if ip not in self.states:
                        self.states[ip] = CameraState(ip=ip, name=cam_name, group=target_group)
                    else:
                        self.states[ip].group = target_group
                        self.states[ip].name = cam_name

                self._rebuild_ip_list()
                self.save_camera_data()
                self.refresh_group_filter_combo()
                self.populate_table()
                self.update_metrics()
                self.append_clean_log("SUCCESS", f"Toplu içe aktarma tamamlandı: {len(dialog.imported_pairs)} yeni kamera listeye eklendi.")

        def add_single_camera_dialog(self) -> None:
            ip, ok = QInputDialog.getText(self, "Yeni Kamera Ekle", "Kameranın Yerel Ağ IP Adresini Giriniz (Örn: 172.16.45.55):")
            if not (ok and ip.strip()):
                return
            ip = ip.strip()
            valid = extract_valid_ips(ip)
            if not valid:
                QMessageBox.warning(self, "Uyarı", f"Geçersiz IP adresi formatı: {ip}")
                return
            ip = valid[0]
            if ip in self.ip_list:
                QMessageBox.warning(self, "Uyarı", f"{ip} adresi listede zaten mevcut!")
                return

            name, ok_name = QInputDialog.getText(
                self, "Kamera Adı / Konumu (Opsiyonel)",
                f"[{ip}] için açıklayıcı isim girin (Örn: Turnike Giriş 1, Peron Doğu):\n(Boş bırakabilirsiniz)",
                QLineEdit.Normal, ""
            )
            cam_name = name.strip() if ok_name and name.strip() else ""

            suggested = match_group_for_ip(ip, self.block_rules, default_group=VARSAYILAN_GRUP)
            group_names = self.get_all_group_names()
            if suggested not in group_names:
                group_names.append(suggested)
            idx = group_names.index(suggested) if suggested in group_names else 0

            target_group, ok2 = QInputDialog.getItem(
                self, "İstasyon / Grup Seç",
                f"{ip} kamerasını hangi gruba ekleyelim?\n(IP Bloğuna Göre Önerilen: {suggested})",
                group_names, idx, False
            )
            if not ok2:
                target_group = suggested

            if cam_name:
                self.camera_names[ip] = cam_name

            self.groups.setdefault(target_group, [])
            self.groups[target_group].append(ip)
            self._rebuild_ip_list()
            self.states[ip] = CameraState(ip=ip, name=cam_name, group=target_group)
            self.save_camera_data()
            self.refresh_group_filter_combo()
            self.populate_table()
            self.update_metrics()

            name_info = f" ({cam_name})" if cam_name else ""
            self.append_clean_log("INFO", f"Yeni kamera eklendi: {ip}{name_info} ➜ '{target_group}' grubu")

        def get_selected_rows(self) -> List[int]:
            rows = set()
            for index in self.table.selectedIndexes():
                rows.add(index.row())
            return sorted(list(rows))

        def _remove_ip_from_all(self, ip: str) -> None:
            if ip in self.ip_list:
                self.ip_list.remove(ip)
            for g in self.groups.values():
                if ip in g:
                    g.remove(ip)
            self.camera_names.pop(ip, None)
            self.states.pop(ip, None)

        def remove_single_camera_by_ip(self, ip: str) -> None:
            cam_name = self.camera_names.get(ip, "")
            name_part = f" ({cam_name})" if cam_name else ""
            reply = QMessageBox.question(self, "Kamera Silme Onayı",
                f"{ip}{name_part} kamerasını listeden silmek istediğinize emin misiniz?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.Yes:
                self._remove_ip_from_all(ip)
                self.save_camera_data()
                self.refresh_group_filter_combo()
                self.populate_table()
                self.update_metrics()
                self.clean_logger.update_active_faults_summary(self.states)
                self.append_clean_log("WARNING", f"Kamera listeden silindi: {ip}{name_part}")

        def remove_selected_cameras(self) -> None:
            selected_rows = self.get_selected_rows()
            visible_ips = self.get_filtered_ips()
            targets = [visible_ips[r] for r in selected_rows if r < len(visible_ips)]
            if not targets:
                curr = self.table.currentRow()
                if 0 <= curr < len(visible_ips):
                    targets = [visible_ips[curr]]
            if not targets:
                QMessageBox.information(self, "Bilgi", "Lütfen silmek istediğiniz kamerayı tablodan seçiniz.")
                return
            if len(targets) == 1:
                cam_name = self.camera_names.get(targets[0], "")
                name_part = f" ({cam_name})" if cam_name else ""
                confirm_msg = f"{targets[0]}{name_part} kamerasını listeden silmek istediğinize emin misiniz?"
            else:
                preview_list = ", ".join(targets[:5]) + ("..." if len(targets) > 5 else "")
                confirm_msg = f"Seçili {len(targets)} adet kamerayı listeden silmek istediğinize emin misiniz?\n\nSilinecek Kameralar:\n{preview_list}"
            reply = QMessageBox.question(self, "Kameraları Silme Onayı", confirm_msg, QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.Yes:
                for ip in targets:
                    self._remove_ip_from_all(ip)
                self.save_camera_data()
                self.refresh_group_filter_combo()
                self.populate_table()
                self.update_metrics()
                self.clean_logger.update_active_faults_summary(self.states)
                self.append_clean_log("WARNING", f"Toplam {len(targets)} kamera listeden silindi.")

        def clear_all_cameras(self) -> None:
            if not self.ip_list:
                QMessageBox.information(self, "Bilgi", "Kamera listesi zaten boş.")
                return
            reply = QMessageBox.question(self, "Tüm Listeyi Temizleme Onayı",
                f"DİKKAT: Sistemdeki TÜM kameralar ({len(self.ip_list)} adet) silinecektir!\nBu işlemi onaylıyor musunuz?",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No)
            if reply == QMessageBox.Yes:
                count = len(self.ip_list)
                self.ip_list.clear()
                self.camera_names.clear()
                self.states.clear()
                for g in self.groups:
                    self.groups[g] = []
                self.save_camera_data()
                self.refresh_group_filter_combo()
                self.populate_table()
                self.update_metrics()
                self.clean_logger.update_active_faults_summary(self.states)
                self.append_clean_log("WARNING", f"Tüm kamera listesi sıfırlandı ({count} kamera silindi).")

        def show_table_context_menu(self, pos: QtCore.QPoint) -> None:
            selected_rows = self.get_selected_rows()
            if not selected_rows:
                return
            visible_ips = self.get_filtered_ips()
            target_ips = [visible_ips[r] for r in selected_rows if r < len(visible_ips)]
            if not target_ips:
                return
            menu = QtWidgets.QMenu(self)
            menu.setStyleSheet(ANKARA_METRO_DARK_STYLE)
            first_ip = target_ips[0]
            first_name = self.camera_names.get(first_ip, "")
            first_display = f"{first_name} ({first_ip})" if first_name else first_ip

            act_preview = menu.addAction(f"👁 Canlı Önizle: {first_display}")
            act_preview.triggered.connect(lambda: self.show_preview_dialog(first_ip))

            act_rename = menu.addAction(f"✏️ Kamera Adını Düzenle: {first_display}")
            act_rename.triggered.connect(lambda: self.rename_camera_dialog(first_ip))

            act_copy = menu.addAction(f"📋 IP Kopyala ({first_ip})")
            act_copy.triggered.connect(lambda: QApplication.clipboard().setText(first_ip))
            menu.addSeparator()

            group_names = self.get_all_group_names()
            if group_names:
                move_menu = menu.addMenu("📁 Gruba Taşı")
                move_menu.setStyleSheet(ANKARA_METRO_DARK_STYLE)
                for g in group_names:
                    act_move = move_menu.addAction(f"📁 {g}")
                    act_move.triggered.connect(lambda checked, grp=g: self._quick_move_to_group(target_ips, grp))
                move_menu.addSeparator()
                act_new_grp = move_menu.addAction("➕ Yeni Gruba Taşı...")
                act_new_grp.triggered.connect(lambda: self._ask_move_to_new_group(target_ips))

            menu.addSeparator()
            act_excel = menu.addAction("📊 Bu Kameraları Excel'e Aktar")
            act_excel.triggered.connect(lambda: self.export_excel_report(only_faults=False))

            menu.addSeparator()
            label = f"🗑 Kamerayı Sil: {first_display}" if len(target_ips) == 1 else f"🗑 Seçili {len(target_ips)} Kamerayı Sil"
            act_del = menu.addAction(label)
            act_del.triggered.connect(self.remove_selected_cameras)
            menu.exec_(self.table.viewport().mapToGlobal(pos))

        def _ask_move_to_new_group(self, ips: List[str]) -> None:
            name, ok = QInputDialog.getText(self, "Yeni Gruba Taşı", "Yeni grup adını girin:", QLineEdit.Normal, "")
            if ok and name.strip():
                name = name.strip()
                self.groups.setdefault(name, [])
                self._quick_move_to_group(ips, name)

        def keyPressEvent(self, event: QtGui.QKeyEvent) -> None:
            if event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
                if self.table.hasFocus():
                    self.remove_selected_cameras()
                    return
            super().keyPressEvent(event)

        def export_ip_list(self) -> None:
            file_path, _ = QFileDialog.getSaveFileName(self, "Kamera IP Listesini Kaydet", "kameralar_yedek.txt", "Metin Dosyaları (*.txt)")
            if file_path:
                try:
                    with open(file_path, "w", encoding="utf-8") as f:
                        f.write(f"# Ankara Metro Kamera IP ve İsim Listesi - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n\n")
                        for g in sorted(self.groups.keys()):
                            ips = self.groups[g]
                            if ips:
                                f.write(f"# === {g} ===\n")
                                for ip in sorted(ips):
                                    name = self.camera_names.get(ip, "")
                                    if name:
                                        f.write(f"{ip}, {name}\n")
                                    else:
                                        f.write(f"{ip}\n")
                                f.write("\n")
                    QMessageBox.information(self, "Başarılı", f"Kamera listesi kaydedildi:\n{file_path}")
                except Exception as e:
                    QMessageBox.critical(self, "Hata", f"Dosya kaydedilemedi: {e}")


class AnkaraMetroCLIMonitor:
    def __init__(self, ip_list: List[str], config: Optional[MonitorConfig] = None,
                 ip_groups: Optional[Dict[str, str]] = None,
                 camera_names: Optional[Dict[str, str]] = None):
        self.ip_list = list(dict.fromkeys(ip_list))
        self.config = config or MonitorConfig()
        self.clean_logger = CleanFaultLogger(log_dir=self.config.log_dir)
        self.network_checker = NetworkChecker()
        self.video_analyzer = VideoStreamAnalyzer(self.config)
        self.ip_groups = ip_groups or {}
        self.camera_names = camera_names or {}
        self.states: Dict[str, CameraState] = {
            ip: CameraState(
                ip=ip,
                name=self.camera_names.get(ip, ""),
                group=self.ip_groups.get(ip, VARSAYILAN_GRUP)
            ) for ip in self.ip_list
        }
        self._state_lock = threading.Lock()
        self._running = False

    def check_single_camera(self, ip: str) -> CheckResult:
        ping_ok, ping_ms, ping_err = self.network_checker.ping(ip, timeout_ms=self.config.ping_timeout_ms)
        if not ping_ok:
            return CheckResult(ip=ip, is_ping_ok=False, is_stream_ok=False, is_frozen=False,
                               error_message=ping_err, ping_time_ms=ping_ms)
        stream_ok, is_frozen, stream_err = self.video_analyzer.analyze_stream(ip)
        error_msg = stream_err if not stream_ok or is_frozen else ""
        return CheckResult(ip=ip, is_ping_ok=True, is_stream_ok=stream_ok, is_frozen=is_frozen,
                           error_message=error_msg, ping_time_ms=ping_ms)

    def process_camera_result(self, result: CheckResult) -> None:
        with self._state_lock:
            state = self.states[result.ip]
            state.last_check_time = datetime.now()
            name_disp = f"{state.name} ({result.ip})" if state.name else result.ip
            if result.is_healthy:
                if state.status == "ARIZALI":
                    self.clean_logger.log_recovery(result.ip, result.ping_time_ms, group=state.group, name=state.name)
                state.consecutive_failures = 0
                state.status = "ONLINE"
                state.last_error = ""
                state.first_failure_time = None
            else:
                state.consecutive_failures += 1
                state.last_error = result.error_message
                if state.first_failure_time is None:
                    state.first_failure_time = datetime.now()
                if state.consecutive_failures >= self.config.consecutive_fail_limit:
                    is_new = (state.status != "ARIZALI")
                    state.status = "ARIZALI"
                    if is_new:
                        self.clean_logger.log_fault(ip=result.ip, error_msg=result.error_message,
                                                     fail_count=state.consecutive_failures,
                                                     first_time=state.first_failure_time, group=state.group,
                                                     name=state.name)
                else:
                    state.status = "UYARI"
                    print(f"[{datetime.now().strftime('%H:%M:%S')}] [UYARI] {name_disp} ({state.consecutive_failures}/3): {result.error_message}")

    def run_check_cycle(self) -> None:
        threads = []
        cycle_results: Dict[str, CheckResult] = {}
        results_lock = threading.Lock()
        def worker(ip_addr: str):
            try:
                res = self.check_single_camera(ip_addr)
                with results_lock:
                    cycle_results[ip_addr] = res
                self.process_camera_result(res)
            except Exception as e:
                print(f"Hata [{ip_addr}]: {e}")
        for ip in self.ip_list:
            t = threading.Thread(target=worker, args=(ip,), daemon=True)
            threads.append(t)
            t.start()
        for t in threads:
            t.join()
        self.clean_logger.log_scan_cycle(self.states, cycle_results)
        self.clean_logger.update_active_faults_summary(self.states)

    def print_summary_status(self) -> None:
        print("\n" + "=" * 95)
        print(f" ANKARA METRO KAMERA KONTROL - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 95)
        print(f"{'KAMERA IP':<16} | {'KAMERA ADI / KONUM':<20} | {'GRUP':<18} | {'DURUM':<10} | {'SON HATA / AÇIKLAMA'}")
        print("-" * 95)
        with self._state_lock:
            for ip in sorted(self.states.keys()):
                st = self.states[ip]
                tag = f"[{st.status}]"
                cname = st.name if st.name else "-"
                err = st.last_error if st.last_error else "Sağlıklı"
                print(f"{ip:<16} | {cname:<20} | {st.group:<18} | {tag:<10} | {err}")
        print("=" * 95 + "\n")

    def start_monitoring(self) -> None:
        self._running = True
        print(f"[BAŞLATILDI] Ankara Metro Kamera Kontrol Servisi Devrede. Toplam Kamera: {len(self.ip_list)}, Tarama Aralığı: {self.config.check_interval_sec}sn")
        try:
            while self._running:
                cycle_start = time.time()
                self.run_check_cycle()
                self.print_summary_status()
                elapsed = time.time() - cycle_start
                sleep_time = max(1.0, self.config.check_interval_sec - elapsed)
                for _ in range(int(sleep_time)):
                    if not self._running:
                        break
                    time.sleep(1)
        except KeyboardInterrupt:
            print("\n[DURDURULDU] Kullanıcı isteğiyle program durduruldu.")
        finally:
            self._running = False


def main() -> None:
    parser = argparse.ArgumentParser(description="Ankara Metro Kamera Kontrol - Pelco IP Kamera Takip Sistemi")
    parser.add_argument("--cli", action="store_true", help="GUI yerine konsol / terminal modunda çalıştırır.")
    parser.add_argument("--once", action="store_true", help="Sadece 1 kez tarama yapıp özet yazdırır ve çıkar.")
    parser.add_argument("--group", type=str, default="", help="Sadece belirtilen istasyon/grubu tarar (Örn: 'Milli Kütüphane').")
    parser.add_argument("--export-excel", type=str, default="", help="Tarama sonucunu belirtilen Excel (.xls / .csv) dosyasına kaydeder.")
    parser.add_argument("--interval", type=int, default=30, help="Tarama döngü süresi (saniye, varsayılan: 30)")
    parser.add_argument("--stream", type=str, default="stream1", help="RTSP akış adı (varsayılan: stream1)")
    args = parser.parse_args()

    if not args.cli and not args.once and not args.export_excel and not args.group:
        if PYQT5_AVAILABLE:
            if hasattr(QtCore.Qt, "AA_EnableHighDpiScaling"):
                QApplication.setAttribute(QtCore.Qt.AA_EnableHighDpiScaling, True)
            if hasattr(QtCore.Qt, "AA_UseHighDpiPixmaps"):
                QApplication.setAttribute(QtCore.Qt.AA_UseHighDpiPixmaps, True)
            app = QApplication(sys.argv)
            app.setStyle("Fusion")
            window = AnkaraMetroMainWindow()
            window.show()
            sys.exit(app.exec_())
        else:
            print("[BİLGİ] PyQt5 bulunamadığı için otomatik olarak CLI moduna geçiliyor.")

    ip_list = VARSAYILAN_KAMERA_IPLERI
    ip_groups: Dict[str, str] = {}
    camera_names: Dict[str, str] = copy.deepcopy(VARSAYILAN_KAMERA_ISIMLERI)

    if os.path.exists(KAMERA_AYAR_DOSYASI):
        try:
            with open(KAMERA_AYAR_DOSYASI, "r", encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, list) and loaded:
                ip_list = loaded
            elif isinstance(loaded, dict):
                all_ips = []
                for grp_name, ips in loaded.get("gruplar", {}).items():
                    all_ips.extend(ips)
                    for ip in ips:
                        ip_groups[ip] = grp_name
                grupsuz = loaded.get("grupsuz", [])
                all_ips.extend(grupsuz)
                for ip in grupsuz:
                    ip_groups[ip] = VARSAYILAN_GRUP
                if all_ips:
                    ip_list = list(dict.fromkeys(all_ips))
                if "kamera_isimleri" in loaded and isinstance(loaded["kamera_isimleri"], dict):
                    camera_names = loaded["kamera_isimleri"]
        except Exception:
            pass

    if args.group:
        filtered_ips = [ip for ip, grp in ip_groups.items() if grp.lower() == args.group.lower()]
        if filtered_ips:
            ip_list = filtered_ips
            print(f"[BİLGİ] '{args.group}' grubu filtrelendi ({len(ip_list)} kamera).")
        else:
            print(f"[UYARI] '{args.group}' adında bir grup bulunamadı! Mevcut tüm kameralar taranacak.")

    config = MonitorConfig(rtsp_stream_path=args.stream, check_interval_sec=args.interval)
    monitor = AnkaraMetroCLIMonitor(ip_list=ip_list, config=config, ip_groups=ip_groups, camera_names=camera_names)

    if args.once or args.export_excel or args.group:
        print("[BİLGİ] Tek seferlik tarama başlatılıyor...")
        monitor.run_check_cycle()
        monitor.print_summary_status()
        if args.export_excel:
            out_file = args.export_excel
            if out_file.lower().endswith(".csv"):
                count = ExcelReportExporter.export_to_csv(out_file, monitor.states, only_faults=False)
            else:
                if not out_file.lower().endswith(".xls"):
                    out_file += ".xls"
                count = ExcelReportExporter.export_to_excel_xml(out_file, monitor.states, only_faults=False)
            print(f"[BAŞARILI] Excel raporu kaydedildi: {out_file} ({count} kamera)")
    else:
        monitor.start_monitoring()


if __name__ == "__main__":
    main()
