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
   - **✅ DOĞRULANDI (14 Eylül, deploy sonrası canlıda test edildi).** Yahoo Finance GitHub Actions'tan
     sorunsuz erişilebiliyor — `sync_prices()` ilk gerçek çalıştırmada **4/4 sembolü** başarıyla çekti
     (`prices: refreshed 4/4 symbols` log satırı), Firestore'da WHEAT için 1256 günlük bar (2021→2026)
     doğrulandı. Canlı sitede WHEAT/CORN/SBM/FREIGHT hepsinde birleşik grafik + sağ fiyat ekseni +
     hover tooltip test edildi, çalışıyor. MACRO'nun ayrı TradingView kutusu dokunulmamış hâliyle
     doğrulandı.

9. **✅ UYGULANDI (14 Eylül, aynı gün ikinci düzeltme) — Daily'de fiyat çizgisi görünmüyordu.** Mete'nin
    fark ettiği gerçek eksiklik: eski TradingView widget'ı Daily seçiliyken o günün **canlı/anlık**
    (saatlik mum) hareketini gösteriyordu; birleşik grafik ise sadece günlük kapanışları kullandığı
    için 1 günlük pencerede en fazla 1 nokta düşüyor, çizgi çizilemiyordu (bu bir hata değildi, veri
    çözünürlüğünün doğal bir sonucuydu, ama Mete'nin istediği "canlı" his kayboluyordu). Çözüm:
    `collector.py`'ye ayrı bir `fetch_intraday_series()` eklendi — aynı Yahoo Finance endpoint'inden
    bu kez `range=2d&interval=5m` ile 5 dakikalık barlar çekiliyor, `prices/{product}.intraday`
    alanına yazılıyor, **her toplayıcı çalıştırmasında** (günlük seriyi 4 saatte bir yenileyen
    `PRICE_REFRESH_HOURS` mantığından bağımsız) tazeleniyor — böylece Daily görünümü gerçekten canlıya
    yakın kalıyor. `index.html` tarafında `renderNewsChart()`, seçili aralık `'1D'` ise `bars` yerine
    `intraday` dizisini kullanıyor, diğer aralıklarda (Weekly/Monthly/1Y/5Y) değişiklik yok. Firestore
    kuralı değişmedi (`prices/{product}` zaten okunabilir), sadece yeni bir alan eklendi — ayrıca
    Publish gerekmiyor. **Bu değişiklik henüz canlıda test edilmedi** (toplayıcı yeniden yüklenip
    çalışana kadar `intraday` alanı dolmayacak, o ana kadar Daily'de yine boş/az veri görülebilir).

10. **✅ UYGULANDI (14 Eylül) — Masaüstü ding + bildirim.** Sekme açıkken BREAKING ya da etki ≥ 75
   olay geldiğinde iki tonlu bir "ding" (Web Audio API, harici ses dosyası yok) + tarayıcı
   `Notification`'ı çıkıyor. Var olan 🔔 Alerts butonunun aynı izin/localStorage durumunu kullanıyor —
   ayrı bir açma/kapama eklenmedi. Mobil push (FCM) bu değişiklikten etkilenmedi, aynı şekilde çalışıyor.

11. **Not (14 Eylül) — Ukrayna demiryolu tahıl ihracatı haberi.** 7. maddedeki ürün değerlendirmesinde
    bu haberi (58% düşüş + fazla malın elde kalması, BEARISH) yanlışlıkla Fransa rekolte örneğiyle
    "aynı aile hata" diye nitelemiştim. Mete haklı olarak düzeltti: Fransa örneği gerçek bir hataydı
    (rekolte düşüşü = az arz = BULLISH olmalıydı, ama BEARISH etiketlenmişti). Ukrayna haberi ise
    farklı bir mantık — ihracat düşüp mal elde kalması bir arz fazlası/zayıf talep göstergesi, bu da
    zaten BEARISH'i destekliyor. İkisi ilgisiz, karıştırılmamalı — ✅ düzeltildi, ayrı bir aksiyon
    gerekmiyor.

12. **✅ UYGULANDI (15 Eylül) — buğday geniş jeopolitik/ateşkes haberlerini
    kaçırıyor, çünkü sorgularımız hep "tahıl/ihracat" kelimesi arıyor.** 14 Eylül akşamı (VN 22:00-
    22:05) ZW.Z26 5 dakikada -14 cent (727.75 → 713.75) düştü, 15-20 dk içinde ~720'ye toparlandı.
    Firestore'daki ham `news` verisi (WebFetch ile, tarayıcı o an bağlı değildi) o dakikalarda hiçbir
    qualifying WHEAT haberi göstermiyordu — en yakın WHEAT kaydı 30 dk sonra, etki 40 (eşiğin altı).
    Mete VN saatiyle 22:07'de kendi telefonundaki bir breaking-news uygulamasında şu başlığı buldu:
    *"Trump: Ukraine has agreed not to hit Russian Energy targets. Russia has agreed to do, likewise.
    The World's Diesel price rise is mostly caused by the Russia/Ukraine War, not Iran"* (Washington,
    D.C. · Ukraine/Iran/Middle East etiketli). Zamanlama (düşüş barının tam içinde/hemen ardında) ve
    mekanizma (Rusya-Ukrayna'da kısmi de-eskalasyon sinyali → buğdayın uzun süredir taşıdığı "savaş
    risk primi" satılır → BEARISH) gayet tutarlı; muhtemel gerçek tetikleyici bu.

    **Kök sebep:** Bu başlık hiç "wheat/grain/export" kelimesi geçmiyor ("enerji hedefleri"nden
    bahsediyor), bu yüzden `GOOGLE_NEWS_QUERIES`'teki hiçbir WHEAT ya da "Black Sea grain..." sorgusuna
    takılmıyor — modelin "savaş azalırsa buğday risk primi azalır" çıkarımını yapabilmesi için önce
    haberin toplanması lazım, o da hiç gerçekleşmedi. Ayrıca bu bir Trump açıklaması/birincil kaynak —
    bizim Google News RSS + 31 site feed hattımız böyle anlık açıklamaları genelde saniyeler değil,
    ikincil bir haber makalesi yayınlandıktan sonra (dakikalar-saatler) yakalıyor.

    **✅ Yapıldı (15 Eylül) —** `collector.py`'nin `GOOGLE_NEWS_QUERIES`'ine grain kelimesi aramayan
    yeni bir sorgu eklendi: `Russia Ukraine ceasefire OR truce OR "peace deal" OR "peace plan" OR
    "energy targets" OR "agreed not to strike" OR "agreed not to hit" OR de-escalation`. Böylece
    tahıldan hiç bahsetmeyen ama savaş risk primini etkileyen başlıklar da toplanıp classifier'a
    gidebilecek (classifier zaten "savaş azalması = buğday BEARISH" çıkarımını `SYSTEM_PROMPT`'taki
    mevcut mantıkla yapabiliyor, sorun sadece toplama/kapsama tarafındaydı). **Henüz GitHub'a
    yüklenip deploy edilmedi** — Mete'ye bu turda yeni `collector.py`/`index.html` gönderildi.

13. **✅ UYGULANDI (15 Eylül) — MACRO sorgusu tahvil faizi/Treasury
    haberlerini hiç yakalamıyor.** Aynı gün (14 Eylül) ABD 10 yıllık tahvil faizi 2023'ten beri ilk
    kez %5'i geçti (Brent $108, enflasyon korkusu, bu hafta Fed kararı öncesi — Bloomberg/CNBC/CNN/
    Yahoo Finance doğruladı: Nasdaq -%1.08, S&P -%0.83, Dow -%0.55, VIX +%11.24). Bu, ders kitabı
    tarzı bir MACRO haberiydi ama Firestore'a hiç düşmedi. Sebep net: `GOOGLE_NEWS_QUERIES`'teki
    MACRO sorgusu — `Brent crude OR "Fed rate" OR "dollar index" OR VIX commodities` — içinde
    **"Treasury yield" / "10-year" / "bond" gibi hiçbir kelime yok**, o yüzden bu manşetlerin hiçbiri
    eşleşmedi. **Not:** aynı gün 12. maddede incelenen WHEAT'teki VN 22:00 sert düşüşün bu tahvil
    haberiyle **ilgisi olmadığı ayrıca doğrulandı** — aynı 5 dakikalık pencerede SBM tamamen düz
    kaldı, CORN'da da buğdaydaki gibi bir çöküş yoktu; geniş bir makro/dolar satışı olsaydı tüm
    kompleksi aynı anda vururdu, vurmadı — bu yüzden 12. maddedeki Ukrayna/Trump açıklaması hâlâ en
    güçlü aday olarak duruyor, bu iki bulgu birbirinden bağımsız.

    **✅ Yapıldı (15 Eylül) —** `GOOGLE_NEWS_QUERIES`'e yeni bir MACRO sorgusu eklendi:
    `"Treasury yield" OR "10-year yield" OR "10-year Treasury" OR "bond yield" OR "10-year note" OR
    "Treasury sell-off"`. 12. maddedeki sorguyla aynı oturumda yapıldı. **Henüz GitHub'a yüklenip
    deploy edilmedi.**

14. **"flash haber" formülü: CTE'nin kaçırdığı
    türde ani, tahıl kelimesi içermeyen ama fiyatı vuran haberleri (12. maddedeki Trump örneği gibi)
    yakalamak için genel bir yaklaşım lazım. Sorgu listesini genişletmek (12/13. maddeler) tek başına
    yeterli olmaz — dünyada hangi flash haberin fiyatı hareket ettireceğini önceden tahmin edip her
    ihtimali sorguya yazmak mümkün değil. İki tamamlayıcı fikir var, ikisi birlikte düşünülecek:

    **A) ✅ UYGULANDI (15 Eylül) — Kapsamı genişletmek (proaktif, ama asla tam olmaz):** 12/13.
    maddelerdeki iki yeni sorguya ek olarak, ISW/Karen Braun/Andrey Sizov için kullandığımız
    `rss.xcancel.com` (X köprüsü) yöntemi `SITE_FEEDS`'e yeni bir satır olarak da uygulandı:
    **Walter Bloomberg / "Breaking Market News (X)"** (`rss.xcancel.com/DeItaone/rss`) — piyasa
    masalarının Trump/Fed/jeopolitik gibi birincil-kaynak flash başlıkları Google News'in ikincil
    habere dönüşmesini beklemeden ilk gördüğü hesaplardan biri, tam da 14 Eylül'de Mete'nin telefonda
    yakaladığı türden bir haber. **Henüz GitHub'a yüklenip deploy edilmedi.**

    **B) Fiyat-anomali alarmı (reaktif güvenlik ağı — asıl yeni fikir):** Kaynak listesi ne kadar
    genişlerse genişlesin, dünyanın her flash haberini önceden öngörüp sorguya yazmak imkânsız. Bunun
    yerine sistemin kendisi "fiyat haber olmadan hareket ediyor" durumunu fark etsin: her ürün için,
    son N dakikadaki (örn. 15-30 dk) intraday fiyat değişimi bir eşiği (örn. ürüne göre %1-1.5)
    aşarsa VE aynı pencerede qualifying net-impact bu hareketi açıklamıyorsa (sıfıra yakın, ya da
    yönü ters), News sekmesinde görünür bir uyarı çıksın: *"⚠️ WHEAT 5 dk'da -%1.9 hareket etti,
    açıklayan qualifying haber yok — dış kaynak kontrol et."* Bu, tam olarak Mete'nin bugün elle
    yaptığı şeyi (fiyat grafiğinde tuhaflık fark edip telefondan haber araması) otomatikleştirir —
    hangi haberi kaçırdığımızı bilmesek bile, kaçırdığımızı ANINDA fark ettirir. `collector.py`
    tarafında hesaplanıp Firestore'a yazılabilir (örn. `news_meta/anomalies`) ya da `index.html`
    tarafında zaten elimizdeki `intraday` fiyat verisi + `newsSeries()` net-impact'i karşılaştırılarak
    tamamen istemci tarafında da hesaplanabilir — ek bir backend değişikliği gerekmez, sadece yeni bir
    UI bileşeni. İkinci yol muhtemelen daha hızlı uygulanır.

    **✅ UYGULANDI (15 Eylül) —** Eşik Mete'nin dediği gibi **±%1** (son 15 dakikada). Tamamen
    istemci tarafında (`index.html`): `computePriceAnomalies()` her ürün için son 15 dk'lık intraday
    fiyat hareketini, `renderNewsAnomalyBanner()` ise News sekmesinin en üstünde (özet kutucuklarının
    hemen üstünde) kırmızı bir uyarı şeridi olarak gösteriyor — "⚠️ WHEAT 15 dk'da -%1.9 hareket etti,
    açıklayan qualifying haber yok (net -10) — dış kaynak kontrol et." Açıklama kontrolü: son 60
    dakikadaki qualifying (impact≥50, conf≥50) haberlerin net etkisi, fiyat hareketiyle aynı yönde ve
    en az ±20 değilse "açıklanmadı" sayılıyor. Aynı olay için tekrar tekrar dingletmemek adına
    (ürün × 15-dk zaman dilimi) bazında bir kez, ve sadece 🔔 Alerts açıksa (aynı buton/izin) ding +
    masaüstü bildirimi de gönderiyor. Ek backend değişikliği yok, ek Firestore okuma yok. **Henüz
    GitHub'a yüklenip deploy edilmedi.**

15. **✅ UYGULANDI (15 Eylül, Mete istedi) — Her sabah VN saatiyle 07:00'de otomatik "Morning Report"
    (gün sonu özeti).** İstenen içerik: bir önceki günün özeti — gece çıkan flash haberler, hangi
    ürünü nasıl etkiledi (BULLISH/BEARISH, impact), CBOT futures ne kadar arttı/azaldı. Mete "başka
    bir ekleme olursa değerlendirebiliriz" dedi, içerik listesi genişleyebilir kalsın diye tasarlandı.

    **Nasıl yapıldı:**
    - `collector.py`: yeni `maybe_send_morning_report()`, her çalıştırmada (GitHub Actions zaten
      ~15 dk'da bir çalışıyor) "şu an Vietnam saatiyle 07 mi VE bugün için rapor zaten yazıldı mı"
      kontrolü yapıyor — böyle idempotent, cron'un tam 07:00'i tutturmasına gerek yok, saat 07 içindeki
      herhangi bir çalıştırma yeter, günde bir kez üretiyor. `build_morning_report()` son 24 saatteki
      `news` dokümanlarını okuyup ürün başına net impact/bull/bear/qualifying sayısı hesaplıyor
      (index.html'deki NEWS_MIN_IMPACT/CONF=50/50 ile birebir aynı eşik), `prices/{product}`'tan da
      24 saatlik % fiyat değişimini (`_price_pct_change`, önce intraday sonra daily seri) çıkarıyor;
      ayrıca en yüksek etkili 6 başlığı ekliyor. Sonuç `news_meta/morning_report`'a (canlı, sayfa
      buradan okuyor) ve ayrı bir `morning_reports/{tarih}` dokümanına (geçmiş kaydı, sayfa henüz
      okumuyor) yazılıyor, sonra `push_morning_report()` ile mevcut FCM cihaz listesine "☀️ Morning
      Report — CTE" bildirimi gidiyor (mevcut `push_alerts()` ile aynı altyapı).
    - `index.html`: `news_meta/morning_report`'a `onSnapshot` ile abone olunuyor; rapor bugüne aitse
      (son 20 saat içinde üretilmişse) ve o tarih için kapatılmamışsa, News sekmesinin en üstünde
      ürün başına net/bull/bear/fiyat-% özet satırları ve en önemli 4 başlıkla bir kart çıkıyor,
      sağ üstteki ✕ ile kapatılabiliyor (kapatma o tarihe özel, yarın yeniden çıkar).
    - Firestore kuralı değişikliği gerekmedi: `news_meta/{id}` zaten herkese açık okunabilir durumda
      (mevcut kural), `morning_reports` koleksiyonu sayfa tarafından okunmadığı için kural gerekmiyor.
    **Henüz GitHub'a yüklenip deploy edilmedi.**

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
