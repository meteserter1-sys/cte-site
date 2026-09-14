# CTE Newsfeed — kurulum ve işleyiş (v3)

```
Google News (5 dil) + yayıncı RSS + X köprüleri ──► collector.py (GitHub Actions, 10 dk'da bir)
                                                       │  tekilleştir → Claude ile sınıflandır
                                                       │  → kümele (trend) → 30 gün sakla → push
                                                       ▼
                                               Firestore `news`
                                                       │  onSnapshot
                                                       ▼
                                   CTE → News sekmesi · History snapshot · telefon bildirimi
```

Sayfa hiçbir haberi kendisi puanlamaz; Claude'un verdiği yön / etki / güven / kategori değerlerini
alır, 24 saatlik kayan pencerede toplar ve fiyat-argümanı olarak gösterir.

## Kaynaklar (31 — masayla kararlaştırıldı, 2026-09-11)

**World** — Karadeniz koridoru (EN); Rusça 2, Ukraynaca 1 Google News sorgusu; Interfax, AgroPortal.ua,
BFM.ru (Google News `site:` ile yayıncıya kilitli); War on the Rocks (RSS); ISW (X köprüsü).

**Crop** — SBM/soya, mısır, buğday, USDA-WASDE-export sales, crop weather, ASF/hog herd/feed demand,
"Karen Braun / Andrey Sizov / SovEcon" (EN); Portekizce 2, İspanyolca 2 sorgu; Barchart, AgWeb,
Successful Farming, AgroLatam, Safras, Notícias Agrícolas (`site:`); USDA News, USDA NASS, World Grain,
IGC (RSS); Karen Braun ve Andrey Sizov (X köprüsü `rss.xcancel.com`).

**Macro** — dry bulk / Baltic Dry / freight rates, bunker fuel, Brent / Fed / DXY / VIX (EN).

Listeler `collector.py` başında: `GOOGLE_NEWS_QUERIES` ve `SITE_FEEDS`. Ölü bir feed atlanır, çalıştırma
durmaz; ilk log'da hangi kaynağın boş döndüğüne bak.

## Sınıflandırma kuralları (özet)

* **Sadece dünya tahıl kompleksini oynatan haber.** Küresel arz-talebi şekillendirmeyen ülkelerin iç
  haberleri (Vietnam, Endonezya, Malezya, Guatemala, Nijerya…), tek çiftlik/tek eyalet anekdotları,
  perakende fiyatı, şirket kârı, altın-gümüş-kripto-borsa → **SKIP** (hiç kaydedilmez).
* **Yön = fiyat argümanı**, duygu değil. Rekor Brezilya hasadı = BEARISH; ihracat yasağı = BULLISH.
  Yem talebi: sürü büyümesi = SBM için BULLISH, hastalık/sürü daralması = BEARISH.
* **Kategori:** POLITICS / AGRI / REPORTS / MACRO. **BREAKING** ayrı bayrak (10'da 1'den az).
* **Macro sınırı:** navlun-yakıt, Brent-doğalgaz, Fed-dolar, BRL-ARS, geniş risk-off VIX. Gerisi SKIP.
* **Sıkı tel:** etki < 30 kaydedilmez (`NEWS_MIN_STORE`). Net-impact hesabına etki ≥ 50 ve güven ≥ 50 girer.
* **Trend / kümeleme:** aynı ürün + yön + ülke + 36 saat + özet benzerliği ≥ %50 → aynı olay; bir kez
  etki katar, `trend` = yayıncı sayısı.
* **Geri bildirim:** sayfadaki 👍/👎 `news_feedback`'e yazılır. Son 30 günün oyları her çalıştırmada
  prompt'a örnek olarak eklenir; bir kaynağa 5+ 👎 → kaynak susturulur (`news_meta/muted`).
* **Saklama:** 30 gün (2026-09-14'te 10'dan yükseltildi — 1 aylık fiyat/haber korelasyonunu
  görebilmek için); eskisi her çalıştırmada silinir. Günün okuması History snapshot'ında kalır.

## Yapılacaklar (2026-09-11'de not edildi)

1. **AÇIK, DOĞRULANDI (14 Eylül) — `firebase.json` — `index.html` cache düzeltmesi.** Canlı sitede
   `fetch('/index.html')` ile bizzat kontrol edildi: `Cache-Control: max-age=3600` hâlâ aktif.
   GitHub repo kök dizini de kontrol edildi (`meteserter1-sys/cte-site`) — `firebase.json` gerçekten
   yok, sadece Mete'nin yerel deploy klasöründe olmalı, o yüzden ben bu dosyayı düzenleyemiyorum.
   Her deploy sonrası daha önce ziyaret etmiş tarayıcılar (kendi tarayıcın dahil) sert yenileme
   (Ctrl+Shift+R) yapılmadıkça yeni sürümü göremeden 1 saate kadar eski sürümü göstermeye devam
   ediyor. **Aksiyon (Mete'nin yerel `firebase.json`'una eklemesi gerek):**
   ```json
   { "hosting": { "headers": [{
       "source": "/index.html",
       "headers": [{ "key": "Cache-Control", "value": "no-cache, max-age=0, must-revalidate" }]
   }] } }
   ```
   Zaten bir `hosting.headers` dizisi varsa bu obje o diziye eklenmeli, üzerine yazılmamalı.

2. **Sınıflandırıcı yön (direction) tutarsızlığı — rekolte/hasat haberlerinde.** 11 Eylül akışında
   iki örnek görüldü:
   - "Fransa'da mısır rekoltesi yeni dibe indi" → **BEARISH** etiketlenmiş, ama kural gereği ters
     olmalıydı: rekolte/verim düşüşü = daha az arz = **BULLISH** (tıpkı "rekor hasat = BEARISH"
     kuralının simetriği gibi). Muhtemel gerçek sınıflandırma hatası.
   - Aynı "Hindistan yağlı tohum ihracatı %12,5 düştü" haberi iki farklı kaynaktan geldiğinde biri
     BEARISH biri BULLISH etiketlenmiş — aynı olay için tutarsız yön ataması.
   - Not: Arjantin "El Niño sayesinde rekor 70,5 M ton mısır hasadı" haberi BEARISH doğru
     etiketlenmiş (El Niño → Arjantin'de bol yağış → iyi verim → fazla arz) — bu örnek sorun değil,
     sadece karşılaştırma için.
   - **✅ UYGULANDI (14 Eylül) — `collector.py`'de `SYSTEM_PROMPT`'a açık bir "SYMMETRY RULE" eklendi**:
     düşen rekolte/verim/hasat tahmini = BULLISH, artan/rekor = BEARISH, ikisi karıştırılmasın diye
     modele doğrudan yazıldı ("iyi/kötü haber" hissiyle değil arz daralması/genişlemesiyle karar
     ver). **Önemli:** bu değişiklik henüz yalnızca bu oturumda düzenlenmiş `collector.py` dosyasında
     — GitHub'a yüklenip Actions bir sonraki `workflow_dispatch`'te çalışana kadar etkisiz, ve zaten
     yalnızca BUNDAN SONRA sınıflandırılacak yeni başlıkları etkiler; Fransa örneği gibi geçmişte
     yanlış etiketlenmiş kayıtlar Firestore'da öyle kalır (geriye dönük düzeltme yapılmadı).

3. **Aynı olay, zıt yön — WASDE günü (12 Eylül) örneği.** Aynı kaynaktan (agriculture.com), ~70
   dakika arayla, aynı WASDE verisini (mısır verimi + bitiş stokları kesildi) yorumlayan iki başlık
   birbirine tamamen zıt etiketlenmiş:
   - "USDA Cuts Corn Yield to 178.5 Bu... Nudges Soybeans Slightly Higher" → **BULLISH 75**
   - "USDA Drops 2026/2027 U.S. Corn Yield and Ending Stocks in September WASDE" → **BEARISH 70**

   2. maddedeki (Fransa/SBM) tutarsızlıkla aynı aile — büyük/karmaşık raporları (WASDE gibi çok
   sayılı, çok yönlü tablo içeren) tek başlıktan yorumlarken model bazen zıt sonuca varabiliyor.
   **Dikkat:** Bunu düzeltirken, aynı akışta gördüğümüz meşru ve KORUNMASI gereken bir ayrı boyutu
   karıştırmamak lazım — "sell the fact" başlıkları ("Corn Fades Lower Despite USDA Yield Cut",
   "Corn Slipping Back in Sell the Fact Reaction to USDA Yield Cut") bilerek BEARISH etiketlenmiş,
   çünkü onlar temel veriyi değil GERÇEKLEŞEN FİYAT TEPKİSİNİ anlatıyor (temel bullish olsa da fiyat
   satılabilir) — bu geçerli bir ayrı sinyal, hata değil. Yani düzeltme hedefi: "aynı temel olguyu
   anlatan başlıklar arasında tutarlılık", "temel veri yönü" ile "gerçekleşen fiyat tepkisi yönü"
   arasındaki meşru farkı silmeden.
   - **✅ UYGULANDI (14 Eylül) — aynı `SYSTEM_PROMPT` düzenlemesiyle** bir "MULTI-COMPONENT REPORTS"
     kuralı eklendi: WASDE gibi çok tablolu raporlarda en önemli bileşenler (üretim/verim, bitiş
     stokları) net edilip TEK yön kararına varılacak; aynı olayı anlatan başlıklar normalde aynı
     yönde olmalı; "sell the fact" tarzı gerçek fiyat-tepkisi başlıkları (başlık açıkça fiyat
     tepkisinden bahsediyorsa) istisna olarak zıt kalabilir ama bu varsayılan değil. Şema
     değişmedi (`direction` hâlâ tek alan) — ayrı bir "fiyat tepkisi" alanı eklemek daha büyük bir
     UI değişikliği gerektirir, şimdilik ertelendi. Aynı yayılma notu geçerli: GitHub'a yüklenip
     yeniden deploy edilene kadar etkisiz, yalnızca ileriye dönük.

4. **✅ ÇÖZÜLDÜ / TEK SEFERLİKMİŞ (14 Eylül doğrulandı) — cron-job.org tetikleyicisi 13 Eylül'de
   başarısız olmuştu.** GitHub → Actions sekmesi kontrol edildi: `is:failure` filtresiyle **0 sonuç**
   — son 290 workflow run'ın tamamı ✅ başarılı, ~15 dakikada bir düzenli çalışıyor, en son çalışma
   14 Eylül'de 12 dakika önceydi. 13 Eylül'deki 500 hatası GitHub'ın anlık/tek seferlik bir
   aksaklığıymış, tekrarlamamış. Ekstra aksiyon gerekmiyor.

5. **🔴 AÇIK, KRİTİK — Claude API kredisi (14 Eylül'de tekrar kontrol edildi: $2,15 kaldı, e-postadaki
   $2,43'ten de düşmüş).** Claude Console → Billing'de doğrudan görüldü: **"Auto reload is off. Turn
   it on to keep your API running when your balance reaches zero."** Yani kredi biterse
   `collector.py`'nin Claude sınıflandırma adımı (GitHub Actions'taki `ANTHROPIC_API_KEY`) sessizce
   durur, haber toplama kesilir. Ben kendi başıma kredi satın alamam/auto-reload açamam (ödeme/hesap
   ayarı — Mete'nin onayı ve kendi işlemi gerekiyor). **Aksiyon (Mete): Claude Console → Settings →
   Billing → ya "Buy credits" ile manuel yükle, ya da "Auto reload"u aç.** Bu iş kalan tek gerçekten
   acil madde.

6. **✅ ÇÖZÜLDÜ (14 Eylül) — Brezilya mısır ihracatı düştü haberi, yön doğrulandı: ETİKET DOĞRU.**
   "Exportação de milho recua 8% em 2026" (Investing.com Brasil, BEARISH 45 conf 65) için asıl
   Investing.com makalesi hâlâ doğrudan çekilemedi, ama Google AI Overview + noticiasagricolas.com.br
   üzerinden aynı olayı anlatan başka kaynaklar bulundu ve sebep netleşti: **rekabet kaynaklı**, Brezilya'nın
   kendi arzı daralmıyor. "Mais competitiva, Argentina acelera exportações de milho enquanto Brasil
   projeta setembro abaixo de 2025" ve "Exportações de milho fecham agosto 32% abaixo de 2025"
   başlıkları, Arjantin'in daha rekabetçi fiyatlarla alıcıları kaptığını gösteriyor — 2. maddedeki
   "kendi arzı daralması = BULLISH olmalıydı" senaryosu değil, Arjantin örneğiyle aynı "dünyada mısır
   bol, Brezilya'nın pazarlık gücü zayıf" mantığı geçerli. Sonuç: **BEARISH 45 doğru etiketlenmiş**,
   düzeltme gerekmiyor.

7. **✅ ÇÖZÜLDÜ (14 Eylül) — Trailing 24h net impact grafiğinde çizgi ikiye bölünüyordu (FREIGHT'te görülmüştü).**
   Mete'nin kararı nettti: "tek parça çizilecek, doğru, ikiye bölünmesin." Buna göre `newsSeries()`
   ve `newsChartSvg()` (index.html) düzeltildi:
   - `newsSeries()`: art arda iki qualifying olay arası 24 saati (`NEWS_WINDOW_MS`) aşınca artık
     anlamsız bir `{gap:true}` işareti koymak yerine, önceki olayın 24 saatlik penceresinin tam
     boşaldığı ana (`prev + NEWS_WINDOW_MS`) `newsWindowStats()` ile **gerçekten hesaplanmış** bir
     nokta ekliyor (aynı mantık son olaydan "şimdi"ye kadar olan boşluk için de uygulandı).
   - `newsChartSvg()`: `.gap` filtreleme/kalem-kaldırma mantığı tamamen kaldırıldı, path artık
     koşulsuz tek parça `M ... L ... L ...` olarak çiziliyor.
   Sonuç: çizgi her zaman tek parça, ve boşluk anındaki değer uydurma/düz-çizgi değil, gerçekten
   o anda pencerenin ne gösterdiğinin doğru hesaplanmış hali. `node --check` ile syntax doğrulandı.
   Canlı sitede (deploy sonrası) görsel doğrulama yapılacak.

8. **✅ UYGULANDI (14 Eylül) — News sekmesindeki grafik işi: varsayılan ürün, birleşik grafik, İngilizce
   etiketler, fiyat ekseni + hover.** Mete'nin bu turdaki isteği tek tek:
   - Açılış grafiği artık **WHEAT** seçili geliyor (SBM değil) — `newsUI.chartProduct` varsayılanı
     değişti.
   - Net-impact grafiği ile CBOT futures fiyat grafiği **tek grafikte birleşti** (WHEAT/CORN/SBM/
     FREIGHT için) — ayrı TradingView kutusu kaldırıldı, sağda kendi ekseni olan gerçek bir fiyat
     çizgisi eklendi. **MACRO dokunulmadı**, DXY/VIX hâlâ ayrı TradingView widget'larında.
     Sebep: TradingView'in embed widget'ı cross-origin iframe olduğu için içine kendi verimizi
     çizemiyorduk — bu yüzden fiyatı kendi tarafımızda (collector.py → Firestore → grafik) çekmeye
     geçildi (aşağıda).
   - Daily/Weekly/Monthly/1Y/5Y aralık etiketleri İngilizce (zaten önceki turda yapılmıştı, bu turda
     tekrar doğrulandı).
   - Fiyat ekseni: seçili aralık penceresindeki **gerçek en düşük/en yüksek kapanış** sağ eksende
     yazılı duruyor (adım aralıklı tik değil, gerçek min/maks). Mouse bir tarihin üzerine gelince
     dikey kılavuz çizgi + o tarihin net-impact ve fiyat değerini gösteren bir tooltip çıkıyor.
   - **Yeni fiyat verisi altyapısı:** `collector.py`'ye `sync_prices()` eklendi — Yahoo Finance'in
     genel chart endpoint'inden (`yfinance` paketinin kullandığı aynı endpoint) WHEAT/CORN/SBM/
     FREIGHT için (`ZW=F`/`ZC=F`/`ZM=F`/`BZ=F`) günlük kapanışları 5 yıllık çekip
     Firestore `prices/{product}` altına yazıyor, en fazla 4 saatte bir sembol başına yeniliyor.
     `firestore.rules`'a yeni `match /prices/{product}` bloğu eklendi (sadece okuma, yazma servis
     hesabından). **Bu kural GitHub Console'dan AYRICA Publish edilmeli** — `firebase deploy --only
     hosting` bunu kapsamıyor (bkz. Kurulum §1).
   - **Doğrulanamayan tek nokta:** Yahoo Finance'in bu endpoint'ine bu oturumun kendi sanal ortamından
     (GitHub Actions değil) erişim engelliydi (proxy + robots.txt kısıtlamaları) — bu GitHub Actions'ın
     kendi ağ erişimini yansıtmıyor, `yfinance` paketinin de aynı endpoint'i kullandığı biliniyor, ama
     ilk gerçek toplayıcı çalışmasından sonra Firestore'da `prices/*` dolduğunu ben ayrıca doğrulayacağım.

9. **✅ UYGULANDI (14 Eylül) — Masaüstü ding + bildirim.** Sekme açıkken BREAKING ya da etki ≥ 75
   olay geldiğinde iki tonlu bir "ding" (Web Audio API, harici ses dosyası yok) + tarayıcı
   `Notification`'ı çıkıyor. Var olan 🔔 Alerts butonunun aynı izin/localStorage durumunu kullanıyor —
   ayrı bir açma/kapama eklenmedi. Mobil push (FCM) bu değişiklikten etkilenmedi, aynı şekilde çalışıyor.

10. **Not (14 Eylül) — Ukrayna demiryolu tahıl ihracatı haberi.** 7. maddedeki ürün değerlendirmesinde
    bu haberi (58% düşüş + fazla malın elde kalması, BEARISH) yanlışlıkla Fransa rekolte örneğiyle
    "aynı aile hata" diye nitelemiştim. Mete haklı olarak düzeltti: Fransa örneği gerçek bir hataydı
    (rekolte düşüşü = az arz = BULLISH olmalıydı, ama BEARISH etiketlenmişti). Ukrayna haberi ise
    farklı bir mantık — ihracat düşüp mal elde kalması bir arz fazlası/zayıf talep göstergesi, bu da
    zaten BEARISH'i destekliyor. İkisi ilgisiz, karıştırılmamalı — ✅ düzeltildi, ayrı bir aksiyon
    gerekmiyor.

## Kurulum — sırayla

### 1. Firestore kuralları
Firebase Console → Firestore → Rules → `newsfeed/firestore.rules` içeriğini yapıştır → Publish.
(`news_feedback` ve `news_devices` yazma izni bu sürümde eklendi; 14 Eylül'de yeni `prices/{product}`
bloğu eklendi — birleşik fiyat grafiği için, ayrıca Publish edilmesi gerekiyor.)

### 2. Secret'lar (yapıldı)
GitHub → Settings → Secrets → `FIREBASE_SERVICE_ACCOUNT`, `ANTHROPIC_API_KEY`.

### 3. Depoyu public yap
GitHub → Settings → General → Danger zone → *Change visibility* → Public. Sebep: Actions dakikası
sınırsız olur, 10 dakikalık periyot ücretsiz çalışır. Kodda gizli bilgi yok; anahtarlar secret'ta.

### 4. Push bildirimi (Android, CTE ana ekrana ekli)
1. Firebase Console → ⚙ Project settings → **Cloud Messaging** → *Web configuration* → **Web Push
   certificates** → *Generate key pair*. Çıkan uzun anahtar **public** anahtardır.
2. `index.html` içinde `const NEWS_VAPID_KEY = '';` satırına yapıştır.
3. `firebase-messaging-sw.js` dosyasını `public/` klasörüne (index.html'in yanına) koy.
4. Deploy et; telefonda News → **🔔 Alerts** → izin ver. Cihaz `news_devices`'a kaydolur.
5. Toplayıcı BREAKING ya da etki ≥ 75 olayları anında push eder (çalıştırma başına en fazla 5).
   Eşik: `NEWS_ALERT_IMPACT` değişkeni.

İkon: bildirimlerde `/icon-192.png` kullanılıyor; `public/` içinde yoksa `manifest.json`'daki ikon
adını `firebase-messaging-sw.js` ve `collector.py` içinde eşle.

### 5. index.html'i yayınla
```
cd "C:\Users\LENOVO\OneDrive\Desktop\cte-site"
firebase deploy --only hosting
```

### 6. İlk çalıştırma
GitHub → Actions → *CTE newsfeed* → **Run workflow**. Log'da: kaç kaynak döndü, kaç haber
sınıflandırıldı, kaçı SKIP/etki<30 elendi, kaç olay yazıldı, kaç bildirim gitti.

### 7. Dış tetikleyici (15-20 dk'da bir, GitHub'ın kendi `schedule:`'ı yerine)

GitHub'ın `schedule:` tetikleyicisi sık aralıklarda güvenilir değil — bu repoda gerçek çalışma
aralıkları 30 dk ayarına rağmen 2-5 saate kadar açıldığı ölçüldü (2026-09-11). Çözüm: dışarıdan,
GitHub'ın `workflow_dispatch` REST endpoint'ini düzenli çağıran ücretsiz bir servis (örn.
**cron-job.org**) kurmak. `.github/workflows/newsfeed.yml` içindeki `workflow_dispatch:` zaten
bunun için hazır — hiçbir kod değişikliği gerekmiyor.

**Bunu SEN yapmalısın** — bir Personal Access Token, bir şifre/anahtar gibi bir kimlik bilgisidir;
Claude bunu senin adına oluşturup üçüncü parti bir siteye giremez.

1. GitHub → sağ üstteki profil resmi → **Settings** → en altta **Developer settings** →
   **Personal access tokens** → **Fine-grained tokens** → **Generate new token**.
   - Repository access: **Only select repositories** → `cte-site` seç.
   - Permissions → **Actions**: **Read and write** (workflow'u tetiklemek için gereken tek izin).
   - Süre (Expiration): istediğin kadar; 1 yıl makul bir varsayılan.
   - **Generate token** → çıkan `github_pat_...` değerini kopyala (bir daha gösterilmez).
2. [cron-job.org](https://cron-job.org) → ücretsiz hesap aç → **Create cronjob**:
   - Title: `CTE newsfeed trigger`
   - URL: `https://api.github.com/repos/meteserter1-sys/cte-site/actions/workflows/newsfeed.yml/dispatches`
   - Schedule: **Every 15 minutes** (veya 20) — saat başı hizalamak şart değil.
   - Request method: **POST**
   - Headers (Advanced → Headers'a ekle):
     - `Accept: application/vnd.github+json`
     - `Authorization: Bearer <1. adımda kopyaladığın token>`
     - `Content-Type: application/json`
   - Body (Advanced → Request body): `{"ref":"main"}`
   - Save.
3. Doğrulama: cron-job.org'da birkaç dakika bekle → job'ın "Last execution" durumu **204 No
   Content** göstermeli (GitHub'ın workflow_dispatch başarı cevabı budur). GitHub → Actions →
   *CTE newsfeed* sekmesinde de yeni çalıştırmalar "Manually run by ..." değil, actor olarak
   token'ı oluşturduğun kullanıcı adıyla ama **Event: workflow_dispatch** görünecek.

**Maliyet notu (Actions dakikası, private repo):** her çalıştırma gerçekte ~35-40 saniye sürüyor
ama GitHub dakikaya yuvarlayıp faturalıyor → çalıştırma başına 1 dk sayılır (ara sıra arka arkaya
gelen olay yoğunluğunda 2-4 dk'ya çıkabiliyor, run #12 ve #2'de olduğu gibi). Free plan (private
repo) sınırı **2.000 dk/ay**:

| Sıklık | Ay içinde çalıştırma | Tahmini dakika | Ücretsiz sınırın altında mı? |
|---|---|---|---|
| 30 dk (mevcut) | ~1.440 | ~1.440 dk | Evet, rahat |
| 20 dk | ~2.160 | ~2.160 dk | Hayır — ~160 dk aşım (~1-2 $/ay) |
| 15 dk | ~2.880 | ~2.880 dk | Hayır — ~880 dk aşım (~7 $/ay) |

Aşım ancak GitHub hesabında bir **spending limit** ayarlıysa ücretlendirilir; ayarlı değilse dakika
biterse çalıştırmalar o ay için sessizce durur (tam da geri istemediğimiz "habersiz kalma" durumu).
Settings → Billing → Plans and usage → Spending limit'i kontrol et.

En temiz çözüm: repoyu **public** yapmak (README'nin 3. adımı zaten bunu öneriyordu ama hiç
uygulanmamış — repo hâlâ private). Public repoda Actions dakikası sınırsız ve ücretsizdir; kodda
gizli bilgi yok, tüm anahtarlar zaten GitHub Secrets'ta. Bunu istersen Claude'a söyle, Settings →
Danger zone'dan tek tıkla yapılabilir bir ayar değişikliği — ama hesap ayarı olduğu için önce senin
onayın gerekiyor.

## Ayarlar (GitHub → Settings → Variables; koda dokunmadan)

| Değişken | Varsayılan | Anlamı |
|---|---|---|
| `CLAUDE_MODEL` | `claude-sonnet-5` | sınıflandırıcı model |
| `NEWS_MAX_NEW` | 80 | çalıştırma başına en fazla sınıflandırılan haber (maliyet tavanı) |
| `NEWS_MIN_STORE` | 30 | bu etkinin altı kaydedilmez |
| `NEWS_RETENTION_DAYS` | 30 (14 Eylül'de 10'dan yükseltildi) | saklama süresi |
| `NEWS_ALERT_IMPACT` | 75 | push eşiği (BREAKING her zaman) |

Sayfa tarafı: `NEWS_MIN_IMPACT = 50`, `NEWS_MIN_CONF = 50`, `NEWS_HISTORY_DAYS = 10` (`index.html`).
CBOT tatil listesi (`CBOT_HOLIDAYS`) ve WASDE/FOMC tarihleri (`NEWS_FIXED_EVENTS`) her Aralık güncellenir.

## Maliyet

Firebase Spark (0 $): günde ~150-300 yazma, 1-3 bin okuma, yılda ~80 MB. GitHub Actions (public, 0 $).
Claude: yalnızca yeni ve tekil başlıklar için, ~0,14 sent/başlık (Sonnet 5). Beklenen 15-25 $/ay;
sert tavan `NEWS_MAX_NEW` ve console'daki 30 $ limit.

**Saklamayı 10 → 30 güne çıkarmanın maliyeti (14 Eylül'de ölçüldü, karar verilirken):** Firestore'da
canlı sayım yapıldı — şu an 303 doküman, ~5,6 günlük veri, yani gerçek yazma hızı günde **~54**
(README'deki "150-300" ilk günkü kaba tahminmiş, gerçek daha düşük çıktı). 30 günde ~1.620 doküman
bekleniyor: depolama ~birkaç MB (Spark'ın 1 GiB ücretsiz sınırının çok altında), okuma sayfa
açılışı başına ~1.600'e çıkar ama bu da günlük 50.000 okuma ücretsiz sınırının çok altında (günde
~30'dan az sayfa açılışı olduğu sürece). **Asıl maliyet kalemi olan Claude sınıflandırma ücreti
saklama süresinden tamamen bağımsız** — sadece yeni gelen başlık sayısına bağlı, o değişmedi.
Sonuç: **ek maliyet pratikte sıfır**, hâlâ Firebase Spark (ücretsiz) sınırları içinde kalıyoruz.

## Yerelde deneme

```
pip install -r newsfeed/requirements.txt
python newsfeed/collector.py --dry-run --mock      # Claude'suz, Firestore'suz
set ANTHROPIC_API_KEY=...
python newsfeed/collector.py --dry-run             # gerçek sınıflandırma, yazmadan
```
