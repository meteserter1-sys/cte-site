# CTE Newsfeed — kurulum ve işleyiş (v3)

```
Google News (5 dil) + yayıncı RSS + X köprüleri ──► collector.py (GitHub Actions, 10 dk'da bir)
                                                       │  tekilleştir → Claude ile sınıflandır
                                                       │  → kümele (trend) → 10 gün sakla → push
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
* **Saklama:** 10 gün; eskisi her çalıştırmada silinir. Günün okuması History snapshot'ında kalır.

## Kurulum — sırayla

### 1. Firestore kuralları
Firebase Console → Firestore → Rules → `newsfeed/firestore.rules` içeriğini yapıştır → Publish.
(`news_feedback` ve `news_devices` yazma izni bu sürümde eklendi.)

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
| `NEWS_RETENTION_DAYS` | 10 | saklama süresi |
| `NEWS_ALERT_IMPACT` | 75 | push eşiği (BREAKING her zaman) |

Sayfa tarafı: `NEWS_MIN_IMPACT = 50`, `NEWS_MIN_CONF = 50`, `NEWS_HISTORY_DAYS = 10` (`index.html`).
CBOT tatil listesi (`CBOT_HOLIDAYS`) ve WASDE/FOMC tarihleri (`NEWS_FIXED_EVENTS`) her Aralık güncellenir.

## Maliyet

Firebase Spark (0 $): günde ~150-300 yazma, 1-3 bin okuma, yılda ~80 MB. GitHub Actions (public, 0 $).
Claude: yalnızca yeni ve tekil başlıklar için, ~0,14 sent/başlık (Sonnet 5). Beklenen 15-25 $/ay;
sert tavan `NEWS_MAX_NEW` ve console'daki 30 $ limit.

## Yerelde deneme

```
pip install -r newsfeed/requirements.txt
python newsfeed/collector.py --dry-run --mock      # Claude'suz, Firestore'suz
set ANTHROPIC_API_KEY=...
python newsfeed/collector.py --dry-run             # gerçek sınıflandırma, yazmadan
```
