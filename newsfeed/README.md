# CTE Newsfeed — kurulum

Mimari (TradersEQ ile aynı mantık):

```
Google News RSS + seçilmiş siteler ──► collector.py (GitHub Actions, 30 dk'da bir)
                                          │  tekilleştir → Claude ile sınıflandır
                                          ▼
                                  Firestore `news` koleksiyonu
                                          │  onSnapshot
                                          ▼
                              CTE → News sekmesi (index.html)
```

Sayfa hiçbir haberi kendisi puanlamaz; Claude'un verdiği yön / etki / güven değerlerini
alır, 24 saatlik kayan pencerede toplar ve fiyat-argümanı olarak gösterir.

## 1. Firestore kuralları (Firebase Console → Firestore → Rules → Publish)

`newsfeed/firestore.rules` dosyasındaki içeriği yapıştır. Mevcut `app_data/main` ve `logs`
kuralların aynen korunuyor; sadece `news` ve `news_meta` için **salt okuma** eklendi.
Yazma tamamen kapalı — collector servis hesabıyla yazdığı için kurallara takılmaz.

## 2. Firebase servis hesabı anahtarı

Firebase Console → ⚙ Project settings → **Service accounts** → *Generate new private key*.
İnen JSON dosyasının **tamamını** birazdan GitHub'a secret olarak yapıştıracaksın.
Bu dosyayı asla `index.html`'e ya da herkese açık bir depoya koyma.

## 3. Claude API anahtarı

https://console.anthropic.com → API Keys → *Create key*. (Chat aboneliğinden bağımsız,
kullandıkça ödemeli. Günde ~500 başlık için aylık birkaç dolar.)

## 4. GitHub deposu

1. GitHub'da **private** bir depo aç (ör. `cte-site`).
2. Bu klasör yapısını depoya koy:
   ```
   .github/workflows/newsfeed.yml
   newsfeed/collector.py
   newsfeed/requirements.txt
   newsfeed/firestore.rules
   newsfeed/README.md
   public/index.html           (isteğe bağlı — deploy için Firebase'e yine terminalden gönderiyorsun)
   ```
3. Depo → **Settings → Secrets and variables → Actions → New repository secret**:
   * `FIREBASE_SERVICE_ACCOUNT` → 2. adımdaki JSON'un tamamı
   * `ANTHROPIC_API_KEY` → 3. adımdaki anahtar
4. İsteğe bağlı **Variables**: `CLAUDE_MODEL` (varsayılan `claude-sonnet-4-5`),
   `NEWS_MAX_NEW` (çalıştırma başına en fazla kaç yeni haber sınıflandırılsın, varsayılan 80).
5. Depo → **Actions** → *CTE newsfeed* → **Run workflow** ile ilk çalıştırmayı elle tetikle.
   Log'da `wrote N docs to Firestore` görmelisin. Sonrası her 30 dakikada otomatik.

> Not: Private depolarda GitHub Actions ücretsiz kotası ayda 2.000 dakikadır. Her çalıştırma
> ~1 dk sürer; 30 dk'lık periyot ayda ~1.450 dk eder — sınırın altında. Daha sık istersen
> depoyu public yap (kodda gizli bir şey yok, anahtarlar secret'ta) ya da cron'u seyrelt.

## 5. index.html'i yayınla

Yeni `index.html`'i `C:\Users\LENOVO\OneDrive\Desktop\cte-site\public\index.html` üzerine kaydet:

```
cd "C:\Users\LENOVO\OneDrive\Desktop\cte-site"
firebase deploy --only hosting
```

News sekmesi, Firestore'da veri oluştuğu an kendiliğinden dolar.

## Yerelde deneme (isteğe bağlı)

```
pip install -r newsfeed/requirements.txt
python newsfeed/collector.py --dry-run --mock      # Claude'suz, Firestore'suz: sadece toplayıp yazdırır
set ANTHROPIC_API_KEY=...                           # Windows cmd
python newsfeed/collector.py --dry-run             # gerçek sınıflandırma, yazmadan
```

## Ayarlanabilir şeyler

* `GOOGLE_NEWS_QUERIES` ve `SITE_FEEDS` → `collector.py` başında; sorgu ekle/çıkar.
* Eşikler → `index.html` içinde `NEWS_MIN_IMPACT = 50`, `NEWS_MIN_CONF = 50`.
* Sınıflandırma kuralları → `collector.py` içindeki `SYSTEM_PROMPT` (yön = fiyat argümanı,
  ürün tanımları, etki ölçeği). Skorlar tutarsız gelirse önce burayı düzelt.


## Maliyet notu (Spark planı)

Toplayıcı, daha önce gördüğü haber id'lerini GitHub Actions önbelleğinde (`newsfeed/.cache/seen_ids.json`) tutar;
Firestore'a sadece hiç görmediği id'leri sorar. Sayfa en fazla 300 haber çeker. Bu ayarlarla günlük kullanım
yaklaşık: 300-1.000 yazma, 1-3 bin okuma, ~1 MB depolama — Spark kotasının (20k yazma / 50k okuma / 1 GB) çok altında.
