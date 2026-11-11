# QEL Language Benchmark VERDICT — Rust vs Zig vs Goose (2026-11-11)

**Karar: GÖÇ YOK.** Bu rapor ölçüm kanıtıdır, mimari karar değildir. Hiçbir üretim kodu
değiştirilmedi, merge yapılmadı. Tüm işler `~/quant/quant/bench-lang-2026-11/`
(isolated scratch, QEL üretim ağacında değil) altında. Repo içi aynası:
`reports/benchmarks/language-2026-11/` (untracked, commit yok).

## Ortam ve sürüm sabitleri
Debian 13, kernel 6.12.115+deb13.1, 6 vCPU Xeon E5-2680 v2, 12 GiB RAM, 160 GB SSD
(%44). rustc 1.99.1 stable, cargo 1.99.1, `link-arg=-fuse-ld=lld`.
Zig 0.17.1 stable (`/home/quant/local/opt/zig-0.17.1`, 412M). Goose a12523f
(binary runtime d521138194f1, 5.6M), AOT `--cc clang` 19.1.7, cmake 3.31.6,
gcc 14.2.0. QEL commit `3678c6a`. Soak (soak.sh, 2026-11-08'den beri) hiç
durdurulmadı.

## Parite (L1, hepsi byte-identical)
Python Oracle'a karşı **boundary (24 olay) + N=1000 + N=1M + N=10M** mutabakata
tüm dilde de doğrulandı (`PARITY.json` + `raw/l3-parity-rerun.json`):

| contender | boundary | 1M | 10M |
|---|---|---|---|
| Rust release | ✅ | ✅ | ✅ |
| Zig ReleaseFast | ✅ | ✅ | ✅ |
| Zig ReleaseSafe | ✅ | ✅ | ✅ |
| Goose AOT (clang) | ✅ | ✅ | ✅ |
| Goose JIT (TinyCC) | ✅ | ✅ | ✅ |

JIT == AOT == Oracle (hash `6a5e85530a9ec848` 1M'de). JIT/AOT arasında sapma
yok → doğruluk hatası yok. Zig'in bench harness'ında stdout/stderr ayrımı
log-hatası (Zig `std.debug.print` stderr'e yazar) denemenin kendinden değil;
stderr dahil tekrar koşuda valid.

## L0 toolchain kaba izleri
| | kurulum/kütüphane | local cache | global cache |
|---|---|---|---|
| Rust | rustup 1.2G + cargo registry 1.2G (preexisting) | L1 kernel hedef 0.95–10.6M | `~/.cargo` (paylaşım) |
| Zig | 412M (opt) | 52–57MB (özel deneme başına) | `~/.cache/zig` 91M (preexisting) |
| Goose | binary 5.6M + `~/.goose` 976K (problarla oluştu) | 0.5–1.1MB (`GOOSE_HOME` deneme başına) | — |

Goose bootstrap derleme maliyeti: **UNMEASURED** (binary hazır geldi).

## L1 compile matrisi (medyan wall_s, 5–10 deneme; `results.csv`/`raw/trials.jsonl`)
| phase | Rust | Zig (Debug/Fast) | ZigSafe | Goose AOT |
|---|---|---|---|---|
| cold-dev | **0.74 sn** | 16.7 sn | — | 2.94 sn (debug) |
| cold-opt | **0.83 sn** | 67.3 sn | 81.6 sn (build¹) | 5.50 sn |
| warm no-op | **0.07 sn** | 55.0 sn² | — | 1.21 sn |
| warm edit | 0.64 sn | 56.0 sn² | — | 0.93 sn |
| warm dep | 0.70 sn | 54.2 sn² | — | 1.02 sn |

¹ `runtime-build` ZigSafe ilk derlemesi. ² Zig warm 55 sn: ölçülmüş gerçek; zig 0.17.1
`build-exe` özel global-cache ile katı yeniden derleme davranışı (bu makine/çağrıda).
Vurgu olsun: cold-dev Rust çok hızlıdır ama yalnızca **180 satırlık bağımsız crate**
(bağımlılıksız, tek crate). QEL'de gerçek maliyet L3'tedir.

### Disk: soğuk derleme hedef boyutları (allocated, deneme başına özel cache)
- Rust cold-dev 10.6MB (debug objeleri), cold-opt 0.95MB.
- Zig 53–57MB, Goose 0.5–1.1MB.

## L1 runtime (optimize binary, doğrulanmış, olay/sn)
| contender | 1M ol/sn (medyan) | 10M ol/sn (medyan) |
|---|---|---|
| Rust release | **26.6M** | 30.7M |
| Goose AOT | 24.4M | **32.3M** |
| Zig ReleaseFast | 24.3M | 28.5M |
| Zig ReleaseSafe | 19.0M | 20.3M |

4 dil de 25–30M mertebesinde, aralar yükte yakın (±%15). Rust 1M'de birinci,
Goose 10M'de birinci; fark pratikte anlamsız (tek thread + memory-only, prod
bottleneck değil). Depolama/G/İ gereceği yok: compute-dominated.

### Goose JIT (TinyCC, edit→result; AOT optimizer eşdeğeri DEĞİL)
| phase | wall_s |
|---|---|
| goose-jit-first (fresh GOOSE_HOME) | 0.30 sn |
| goose-jit-warm | 0.27 sn |
| goose-jit-edit (aynı semantik edit + koşu) | 0.33 sn |
| edit hash | değişiklik kanıtı (ded1a4e2→restore canonical) |

JIT 0.3 sn/süpürme (1M olay) başarılı; AOT 1M koşusu ~0.04 sn olduğu için
JIT koşusu AOT'un ~7×ı maliyetlidir ama ilk oturumda 5.5 sn AOT derlemesi
yok. Kalıcılık yok; artifact üretmiyor. Bu**geliştirici dönüşü (edit-to-run)** için bir
sınıf üstünlüğüdür, çalışma zamanı sıralamasına katılmaz.

## L3 (mevcut QEL Rust baseline) — gerçek maliyet bu
| crate | cold build (scratch target) | target boyutu | warm no-op | çekirdek edit² |
|---|---|---|---|---|
| `policy/rust` (13 dep) | 15.9 sn | 155MB (debug) | 0.08 sn | `cargo check` 8.4 sn |
| `native/runner` (178 dep, Nautilus rev 27a8e54) | **2m48s** | **2.40 GB** | 0.79 sn | policy touch 0.62 sn (link dâhil) |

² içerik değişikliği değil, mtime touch (parmak izi yoklaması).

**Kritik bulgu:** Asıl darboğaz L1'deki Rust değil, **Nautilus bağımlı 178 crate'lik
dev graph**. 2.40 GB hedef, 163MB debug binary. Bu birimlerde "Rust 0.7 sn" sayısı
QEL gerçekliğini anlatmaz.

Yüzde 100 soğuk derleme (cargo fetch hariç, ben fetch hazır) 2m48s; daha önce
mevcut `policy/rust/target` 5.4M (kısmi/önceki bir check). Profil varyasyonları (L1 crate, kanıt amaçlı):
- `lto=true`: 12.7 sn vs 0.83 sn — LTO derlemesi katlanarak pahalı, 372KB vs 454KB.
- `strip=true`: 0.745 sn, 345KB (en küçük binar).
- `opt-level='s'`: 0.586 sn, 453KB.
Çıkış hash'i bütün profillerde canonical.

## L2
**UNMEASURED.** L1 paritesi; L2 taker-replay native adaptasyon için atlandı:
QEL değerlendirme önceliği ve makinedeki bağımlılık tekrar maliyetleri
göz önüne alınınca, sonraki adım L3 arkadaşlığı daha büyük etkinin kanıtı.
Parite ispatı olmadan native iddia üretme kuralı gereği UNMEASURED.

## Nitel kısıtlar (sayı değil)
- Parquet/Arrow, C ABI/FFI: Rust yerleşik (maturin etc.), Zig C ABI ideal
  (export C, `@cImport`), Goose `extern fn`/`export fn` + C üretir.
- Deterministik eşzamanlılık ve güvenli model üç dilde de konu.
- cross-platform: Rust > Zig > Goose (Goose yeni, Linux dışı kapsam dar).
- Zig 0.17.1 erken sabitleme ve main imzası güncellemeleri; goose bootstrap UNMEASURED.
- Goose benchmark pazarlamasında "C++'tan hızlı" iddiası kapsama koşuluyla karşılaştırıldı:
  L1'de Goose 30M merit ~Rust eşit; bağımsız, memory-only kernel.

## 8 soruya doğrudan cevap
1. **Soğuk en hızlı compile?** L1 toy: Rust (0.74 sn). QEL gerçekçi: Nautilus runner 2m48s (Rust) — QEL bağlamında Rust "soğuk pahalı".
2. **Edit sonrası hız?** L1: Rust 0.64 sn, Goose AOT 0.93 sn, Goose JIT 0.33 sn (compile+run). Zig 56 sn (bu çağrı). L3: warm rebuilds ~0.6–0.8 sn, `cargo check` 8.4 sn.
3. **Derleme başına disk en az?** Goose (0.5–1.1MB), Rust cold-opt 0.95MB; Zig 53–57MB; L3 runner 2.40GB (söz konusu maliyet).
4. **En küçük optimize binary?** Rust strip 345KB / release 454KB; Goose 87KB; Zig 3.9MB (ReleaseFast, ZigSafe 3.7MB).
5. **Doğrulanmış throughput?** 1M: Rust 26.6M mur/s; 10M: Goose 32.3M. (±%15 içi bağ).
6. **En az RAM?** Derleme peak'i ölçümü `ru_maxrss` kümülatif olduğu için yanlış yorumlanmamalı: büyüklük sırası 312–468MB toplam; çalışma zamanı RSS üç dilde de kod çekirdeği için ~küçük. Tekil RSS sonucu bilinçli UNMEASURED (cgroup peak yok).
7. **Rust profil iyileştirme?** `strip=true` en mantıklı (0.745 sn / 345KB, hash geçerli), `opt-level=s` 0.586 sn; LTO derleme maliyetini ~15× katlıyor (R): LTO'yu varsayılan yapma. Paylaşımlı `CARGO_TARGET_DIR` + `.build-cache` kurulumu zaten mevcut (~3.5GB preexisting) — silme yapılmadı.
8. **Trade-off önerisi:** Göç yok. Goose JIT geliştirici döngüsüne çekilip edit latency kanıtıyla bir aday olarak kalabilir, AOT çalıştırımı Goose artı iken clang değerlendirme ayrıca yapılıyor. Rust'un cezası dil değil, Nautilus graph'ı; paralel hedef kullanımı (`CARGO_TARGET_DIR` bölünmesi), `cargo check` alışkanlığı ve `strip` iyi kombinasyon; `cargo clean`'in 2.4GB hedefte tolere edilmemesi önerilir.

## Pareto (compile süresi × throughput, L1)
- Rust: compile'da en iyi, throughput üst grup.
- Goose: compile ~2.7–5 sn, throughput üst grup; JIT edit latency yıkıcı min.
- Zig: bu çağrı/compilasyonda açık sonuncu; throughput yine rekabetçi.

## Entegrasyon riskleri
- Nautilus'lu Rust kod Zig/Goose'a taşınamaz; bu benchmark taşınabilirliği değil.
- Goose olgunlaşmamış ekosistem (Parquet/Arrow bindings UNMEASURED).
- Zig build-exe warm davranışı bu hostta yoksul.
- Tüm tarafsız kanıt scratch altında, üretim kodu ve branch'e dokunulmadı.

## Sonraki küçük prototip (göç değil)
Bir sonraki kecil prototip: tek sembollü taker-replay challenger, L2 parite testleriyle;
Goose JIT edit döngüsü canlı kullanılarak giriş-gelişim ölçümü, biçimlilik ve güvenlik incelenmesi
bağımsız insan mimari incelemesinden geçerek.
