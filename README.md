# Ankara Metro Kamera Kontrol

Bu yazılım; **Ankara Metrosu** istasyon ve tesislerinde görev yapan Pelco IP kameraların sağlık durumunu, yerel ağ erişilebilirliğini ve video akışlarını takip eden, donan veya arızalanan kameraları anında tespit eden gelişmiş bir Güvenlik ve Kamera İzleme sistemidir.

---

## 🔒 Çevrimdışı (Offline / Air-Gapped) Çalışma Garantisi

> [!IMPORTANT]
> **BU PROGRAM İÇİN KESİNLİKLE İNTERNET BAĞLANTISI GEREKMEZ.**
> 
> - **RTSP Akışı:** Doğrudan kameranın yerel IP'sine (`rtsp://IP/stream1`) portsuz ve doğrudan bağlanır.
> - **Ağ Kontrolü (Ping):** Yalnızca yerel ağdaki (LAN) kamera IP'sine işletim sisteminin yerel ICMP aracıyla ping atar. Dış dünyaya istek göndermez.
> - **Arayüz (PyQt5):** Tamamen bilgisayarınızdaki yerel Qt DLL'leri üzerinden çizim yapar.
> - **Loglama:** Günlük kayıtlar ve anlık arıza özeti sadece bilgisayarınızın yerel sabit diskindeki `logs/` klasörüne yazılır.

---

## 🛠️ Hızlı Kurulum Sistemi (`GEREKSINIMLERI_KUR.bat`)

Yeni bir bilgisayara geçtiğinizde veya çevrimdışı bir ortama kütüphaneleri taşımak istediğinizde:

1. **`GEREKSINIMLERI_KUR.bat`** dosyasına çift tıklayın.
2. Açılan menüden dilediğiniz adımı seçin:
   - **`[1] Hızlı Kurulum`**: Sistemde `kurulum_paketleri` klasörü varsa çevrimdışı, yoksa internetten otomatik kurar.
   - **`[2] Çevrimdışı Paketleri İndir`**: İnternetli bir bilgisayarda çalıştırarak `opencv-python`, `PyQt5` ve `numpy` paketlerini `./kurulum_paketleri/` içine çeker (USB'ye kopyalayıp izole ağa taşımak için).
   - **`[3] Sadece Çevrimdışı Kurulum`**: İnternetsiz izole ortamda `./kurulum_paketleri/` klasöründen kurulumu tamamlar.
   - **`[4] Test Et`**: Kurulum durumunu kontrol eder.

---

## 🚀 Programı Başlatma (Tek Tık - One Click Run)

- Klasördeki **`BASLAT.bat`** dosyasına **çift tıklamanız** yeterlidir!
- Arayüz otomatik olarak açılacaktır.

---

## 🗑️ Kamera Silme Seçenekleri

Listeden kamera silmek son derece esnek ve kolaydır:
1. **Satır İçi Silme Butonu:** Tablodaki her kameranın yanında doğrudan kırmızı bir `[🗑]` silme butonu bulunur.
2. **Çoklu Seçim ve Silme:** Tablodan `Ctrl` veya `Shift` tuşlarıyla birden fazla kamera seçip araç çubuğundaki **`[🗑️ Seçilenleri Sil]`** butonuna tıklayabilirsiniz.
3. **Klavyeden Silme:** Tabloda kamerayı seçip klavyenizdeki **`Delete`** tuşuna basmanız yeterlidir.
4. **Sağ Tık Menüsü:** Kameranın üzerine sağ tıklayarak **`🗑 Kamerasını Sil`** seçeneğini kullanabilirsiniz.
5. **Tümünü Temizle:** Tüm listeyi sıfırlamak için araç çubuğundaki **`[⚠️ Tümünü Temizle]`** butonunu kullanabilirsiniz.

---

## 📂 Toplu IP İçe Aktarma (Bulk Import)

- Araç çubuğundaki **`[📂 Toplu IP İçe Aktar]`** butonuna basın.
- `kameralar_ornek.txt` gibi bir dosya seçebilir ya da Excel/Not Defteri'nden kopyaladığınız IP'leri doğrudan kutuya yapıştırabilirsiniz.
- Sistem geçersiz IP'leri ve mükerrerleri otomatik filtreleyerek listeye ekler ve kalıcı olarak kaydeder.

---

## 📂 Dosya Yapısı

- [`BASLAT.bat`](file:///C:/Users/mehmet/.gemini/antigravity/scratch/pelco_camera_monitor/BASLAT.bat): Tek tıkla programı başlatan dosya.
- [`GEREKSINIMLERI_KUR.bat`](file:///C:/Users/mehmet/.gemini/antigravity/scratch/pelco_camera_monitor/GEREKSINIMLERI_KUR.bat): Kütüphane kurulum asistanı (Online & Çevrimdışı destekli).
- [`ankara_metro_kamera_kontrol.py`](file:///C:/Users/mehmet/.gemini/antigravity/scratch/pelco_camera_monitor/ankara_metro_kamera_kontrol.py): Ana program (GUI + CLI motoru).
- [`kameralar_ornek.txt`](file:///C:/Users/mehmet/.gemini/antigravity/scratch/pelco_camera_monitor/kameralar_ornek.txt): Toplu IP ekleme örnek şablonu.
- `requirements.txt`: Python bağımlılık listesi.
- `cameras.json`: Kayıtlı kameraların yerel ayar dosyası.
- `logs/`:
  - `arizali_kameralar_guncel.txt`: Anlık arızalı kameraların canlı tek sayfalık özeti.
  - `kamera_ariza_YYYY_MM_DD.log`: Günlük temiz arıza ve kurtarma olayları.
# kamerav3
