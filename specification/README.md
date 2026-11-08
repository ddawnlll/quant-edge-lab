# QUANT EDGE LAB · v1.1 uygulama belirtimi

Bu paket v1.0’ı değiştirmeden uygulanabilir sözleşmelerle genişletir. Laboratuvar uygulanmamıştır. Yalnız mimari/şema/izlenebilirlik doğrulaması çalıştırılmıştır; native conformance, benchmark, null-program kalibrasyonu ve gerçek execution henüz yapılmamıştır.

| Dosya | Amaç |
| --- | --- |
| `QUANT_EDGE_LAB_IMPLEMENTATION_MONOGRAPH_v1_1.md` | Türkçe gerekçe, normatif davranış ve nesne kataloğu |
| `spec/architecture.json` | Tek canonical makine giriş noktası |
| `schemas/architecture.schema.json` | Mimari kayıtlarının strict yapısal tipi |
| `schemas/runtime.schema.json` | 102 runtime/model tipi; `$defs` üzerinden referans |
| `schemas/fixtures.schema.json` | Test DSL’sinin strict grameri |
| `fixtures/conformance.json` | F01–F35 somut girdileri ve oracle’ları |
| `fixtures/README.md` | Fixture state injection ve operation anlamları |
| `examples/strategy_mr.json` | Tam typed21node örnek; hash’leri bağlanana kadar çalıştırılamaz |
| `examples/runtime_lock.unresolved.json` | Gerçek feasibility kanıtıyla doldurulacak, açıkça çözümlenmemiş lock |
| `baseline/QUANT_EDGE_LAB_MONOGRAPH_v1_0_TR.html` | Yüklenen v1.0’ın byte-eşit kopyası |
| `QUANT_EDGE_LAB_COVERAGE_AND_VERIFICATION_v1_1.md` | Coverage, sonuçlar ve doğrulanmamış işler |
| `validation/validation_report.json` | Çalıştırılan belirtim kontrolleri ve input hash’leri |
| `validation/validate_spec.py` | Offline yapısal/semantic paket doğrulayıcısı |
| `validation/render_catalog.py` | İnsan belgesinin H-20 kataloğunu canonical JSON’dan üretir |
| `MANIFEST.sha256` | Teslim dosyalarının SHA-256 envanteri |

Bir uygulama ajanına component ve TASK ID verin. Ajan canonical JSON’dan owner/dependency/contract/schema/requirement/test/ADR/risk closure’ını çözer. Bütün belgeyi her işte context’e taşımak gerekmez. Aynı kontratın yerel, ad hoc bir kopyası oluşturulmaz. İlk pahalı karar kapısı TASK-08 native feasibility’dir; UNRESOLVED lock veya UNVERIFIED capability, runtime uyumluluk kanıtı sayılmaz.

Paket doğrulaması (Python3.12+, dependency kurulumu dışında offline):

```sh
python3 -m venv .spec-venv
.spec-venv/bin/python -m pip install -r validation/requirements.txt
.spec-venv/bin/python validation/validate_spec.py --root . --report /tmp/qel-spec-validation.json
```

Yeni rapor `/tmp`’ye yazılır; teslim raporu korunur. Önce teslim bütünlüğü için Linux’ta `sha256sum -c MANIFEST.sha256` kullanılabilir. Şema/custom numeric domain/ID/DAG/anchor kontrolleri ve20 negatif mutant, bir trading engine’in veya istatistik modelinin doğruluk sertifikası değildir.

Specification değiştirildiğinde iki temsil birlikte güncellenir. H-20 için `python3 validation/render_catalog.py`; sonra validator yeniden çalıştırılır. Özgün v1.0 overwrite edilmez. Breaking semantic değişiklik yeni version/certificate/trial gerektirir; tüketilmiş OOS yeniden kullanılamaz.
