# QUANT EDGE LAB v1.1 — Kapsam, izlenebilirlik ve doğrulama

**Sonuç: PASS_SPECIFICATION_ONLY.** Laboratuvar uygulanmamış ve deployment başlatılmamıştır.

Türkçe monograf ile canonical JSON; 15 bileşen, 96 gereksinim, 102 model tipi, 24 kontrat, 18 invariant, 32 mühendislik görevi, 35 fixture ve 9 milestone içerir.

v1.0 aynen korunmuştur. Orijinal SHA-256: `71628e4b4ceceac3447759cdabab131e16237aff9fd7569a09f1bc61e4d70d06`.

## Çalıştırılan kontroller

| Kontrol | Sonuç |
| --- | --- |
| 3 JSON Schema ve makine belgeleri | Geçti |
| ID, model, kontrat, human anchor ve dependency DAG çözümü | Geçti |
| v1.0 kapsamı | 27 bölüm + 3 ek; 30/30 eşlendi |
| Bilerek bozulmuş negatif mutant | 20/20 reddedildi |
| Exact runtime tür vektörleri | 16/16 geçti |
| Referans aritmetiği | 7 kontrol geçti |
| ASCII canonical hash vektörü | 1 kontrol geçti |
| Orijinal dosya bütünlüğü | SHA-256 eşleşti |

Tam sonuç ve doğrulanan dosyaların hash’leri `validation/validation_report.json` içindedir. Bu kontroller runtime correctness, model geçerliliği veya kârlılık kanıtı değildir.

## Açıkça çalıştırılmayanlar

35 native/integration fixture yalnız tanımlanmıştır. B0–B8 ve derleme benchmarkları, native sürüm/ABI/LocalView feasibility, istatistiksel null-program kalibrasyonu, gerçek veri/lisans kapsamı, soak ve forward execution çalıştırılmamıştır. 20 capability UNVERIFIED durumundadır; runtime lock UNRESOLVED’dır. Bunların kapanış yöntemi ve blokladıkları kapılar 14 açık risk kaydında belirtilmiştir.

## Açık amendments

- ADR-04: OOS tüketim/alpha kaydı ayrı evaluator-owned SQLite dosyasında; deneysel erişimden önce bağımsız receipt anchor.
- ADR-15: İptal sonrası miktar özdeşliği `accepted = filled + leaves + canceled`; v1.0 kısa ifadesinin kapsamı düzeltilmiştir.

Diğer ADR’ler devralınan karar, kesinleştirme veya yeni uygulama tercihidir. Custodial kalite erişimi ile deneysel OOS tüketimi ADR-16’da ayrılır. Hiçbir değişiklik v1.0 içine sessizce yazılmamıştır.

## v1.0 → uygulama kapsamı

| Temel bölüm | Konu | İnsan bölümleri | Gereksinimler |
| --- | --- | --- | --- |
| V10-01 | Problem tanımı ve ekonomik amaç fonksiyonu | H-08, H-10 | REQ-001, REQ-002 |
| V10-02 | Araştırma yöntemi ve kanıt hiyerarşisi | H-14 | REQ-004 |
| V10-03 | Literatür ve mevcut sistemler | H-06, H-07 | REQ-005 |
| V10-04 | Üç farklı HFT gereksinimi | H-06 | REQ-021 |
| V10-05 | Gereksinimler ve kapsam sınırı | H-10 | REQ-001 |
| V10-06 | Dil ve teknoloji alternatifleri | H-05, H-06, H-14 | REQ-005, REQ-006, REQ-081 |
| V10-07 | Seçilen sistem mimarisi | H-02, H-03, H-11, H-12 | REQ-007, REQ-011, REQ-062 |
| V10-08 | Veri edinme, piyasa evreni ve kalite | H-04, H-06, H-10 | REQ-012, REQ-013, REQ-014, REQ-015, REQ-016, REQ-017, REQ-018, REQ-094 |
| V10-09 | Az RAM ile büyük veri | H-04, H-05, H-06, H-08 | REQ-012, REQ-016, REQ-023, REQ-024, REQ-025, REQ-093 |
| V10-10 | Canonical veri ve strateji sözleşmeleri | H-03, H-04, H-05, H-06, H-10 | REQ-006, REQ-008, REQ-009, REQ-010, REQ-011, REQ-013, REQ-019, REQ-025, REQ-026 |
| V10-11 | Simülasyon doğruluğu modeli | H-06 | REQ-016, REQ-017, REQ-019, REQ-020, REQ-021, REQ-022 |
| V10-12 | Emir yaşam döngüsü ve matching | H-06, H-07 | REQ-027, REQ-028, REQ-029, REQ-030, REQ-031, REQ-032, REQ-033, REQ-034, REQ-035 |
| V10-13 | Funding, hesap, margin, liquidation ve risk | H-03, H-06, H-07, H-08, H-16 | REQ-002, REQ-003, REQ-010, REQ-034, REQ-036, REQ-037, REQ-038, REQ-039, REQ-040, REQ-041, REQ-042, REQ-043, REQ-093 |
| V10-14 | İstatistiksel araştırma protokolü | H-08, H-09, H-10, H-11 | REQ-044, REQ-045, REQ-046, REQ-047, REQ-048, REQ-049, REQ-050, REQ-051, REQ-052, REQ-053, REQ-054, REQ-055, REQ-056, REQ-092, REQ-093, REQ-094 |
| V10-15 | Karar sistemi ve edge terfi kapıları | H-09, H-10 | REQ-001, REQ-050, REQ-057 |
| V10-16 | Agent-native araştırma mekanizması | H-02, H-10, H-11, H-13 | REQ-007, REQ-008, REQ-056, REQ-058, REQ-059, REQ-060, REQ-061, REQ-094 |
| V10-17 | Araştırma hafızası ve append-only ledger | H-03, H-10, H-11, H-12, H-13 | REQ-009, REQ-051, REQ-055, REQ-063, REQ-064, REQ-065, REQ-066, REQ-067, REQ-096 |
| V10-18 | Bağımsız audit ve correctness | H-08, H-09, H-11, H-14 | REQ-038, REQ-060, REQ-068, REQ-069, REQ-070, REQ-071 |
| V10-19 | 7/24 çalışma ve kaynak yönetimi | H-08, H-10, H-12, H-13 | REQ-042, REQ-056, REQ-072, REQ-073, REQ-074, REQ-075, REQ-076, REQ-077, REQ-096 |
| V10-20 | İnsan gözetimi ve dashboard | H-12 | REQ-078, REQ-079 |
| V10-21 | Performans, derleme ve task-time metodolojisi | H-14 | REQ-004, REQ-069, REQ-080, REQ-081 |
| V10-22 | Forward calibration ve tradebot'a dönüşüm | H-08, H-10, H-13, H-16 | REQ-002, REQ-043, REQ-057, REQ-058, REQ-083, REQ-084, REQ-085, REQ-086 |
| V10-23 | Alternatif kernel için terfi protokolü | H-06, H-14 | REQ-005, REQ-082, REQ-095 |
| V10-24 | Örnek araştırma dosyası: volatility-aware mean reversion | H-08, H-17 | REQ-003, REQ-087 |
| V10-25 | Uygulanabilir geliştirme yol haritası | H-15, H-19 | REQ-088, REQ-089, REQ-091 |
| V10-26 | Riskler, karşı argümanlar ve kalan belirsizlikler | H-06, H-18 | REQ-090, REQ-095 |
| V10-27 | Nihai mimari hüküm | H-06 | REQ-005 |
| V10-A | Ek A. API ve kayıt sözleşmesi | H-03, H-19 | REQ-009, REQ-011, REQ-091 |
| V10-B | Ek B. Benchmark raporu | H-14 | REQ-004, REQ-080, REQ-081 |
| V10-C | Ek C. Kaynakça | H-18 | REQ-090 |

## Ters izleme ve yeniden doğrulama

Her normatif nesne stable ID, human section ve object anchor taşır. `model_bindings` model→schema→owner bağını; task kayıtları dependency/contract/test/negative-case bağını; `original_requirements` R01–R10 geçişini tanımlar. Human H-20 canonical JSON’dan üretilmiştir.

```sh
python3 -m venv .spec-venv
.spec-venv/bin/python -m pip install -r validation/requirements.txt
.spec-venv/bin/python validation/validate_spec.py --root . --report /tmp/qel-spec-validation.json
```

Dependency kurulumu dışında doğrulama offline’dır. Paket üretim kodu içermez; Python dosyaları yalnız belirtim doğrulama ve katalog üretme araçlarıdır. Ekonomik mekanizma, gelecekteki stationarity ve karşı-olgusal impact şemayla kanıtlanamaz; gerekli bağımsız inceleme `non_executable_requirements` kaydındadır.
