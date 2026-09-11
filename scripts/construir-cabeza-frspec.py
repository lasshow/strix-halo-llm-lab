#!/usr/bin/env python3
"""Construye la cabeza MTP FR-Spec 65k A PARTIR DE LA CABEZA UNSLOTH (la que
corre en produccion), no de la conversion de drluoto.

Motivo (H-036 A, primer intento): la cabeza de drluoto es OTRA conversion del
mismo MTP (fc_embd/fc_hidden en vez de eh_proj; hc_norm/down/up en vez de
hc_head_*), asi que nuestra build no la carga. Portar su conversion entera
seria cambiar dos cosas a la vez. Aqui solo cambia lo que H-036 A quiere
medir: el vocabulario del borrador recortado.

Receta:
  - copia todos los tensores y metadatos de la cabeza Unsloth;
  - anade `output.weight` = las 65 536 filas del `output.weight` del TRONCO
    (Q6_K -> dequant F32 -> filas d2t -> requant Q8_0, como la de drluoto);
  - anade `d2t` (I64) tomado de la cabeza de drluoto (su ranking de frecuencia
    es el activo que se reutiliza; la lista de ids es independiente de la
    conversion);
  - deja `nextn_shared_target_tensors` (BOOL) como esta: la cabeza sigue
    tomando `token_embd` del tronco; `output.weight` ahora es propio y nuestra
    carga (H-036 port) lo prefiere al prestado.

Uso: construir-cabeza-frspec.py <tronco shard1> <cabeza unsloth> <cabeza drluoto> <salida>
"""
import sys

import numpy as np

sys.path.insert(0, "/models/llama-builds/df03399b885831b2a1603b3abb0d8c156808e363+6e8170fb/src/gguf-py")
from gguf import GGUFReader, GGUFWriter, GGMLQuantizationType  # noqa: E402
from gguf.quants import dequantize, quantize  # noqa: E402

tronco, unsloth, drl, salida = sys.argv[1:5]

r_drl = GGUFReader(drl)
d2t = np.array([t for t in r_drl.tensors if t.name == "d2t"][0].data).reshape(-1).astype(np.int64)
assert d2t.shape == (65536,) and len(set(d2t.tolist())) == 65536, d2t.shape

# output.weight del tronco (puede estar en cualquier shard; probamos los tres)
import re, os
out_t = None
for i in (1, 2, 3):
    p = re.sub(r"-0000\d-of-", f"-0000{i}-of-", tronco)
    rr = GGUFReader(p)
    for t in rr.tensors:
        if t.name == "output.weight":
            out_t = t
            break
    if out_t is not None:
        break
assert out_t is not None, "output.weight no esta en el tronco"
print("tronco output.weight", list(out_t.shape), out_t.tensor_type.name)
full = dequantize(np.array(out_t.data), out_t.tensor_type)   # [n_vocab, n_embd] en numpy
print("dequant", full.shape, full.dtype)
assert full.shape[0] > 65536
trim = np.ascontiguousarray(full[d2t])                         # [65536, n_embd]
del full
trim_q8 = quantize(trim.astype(np.float32), GGMLQuantizationType.Q8_0)
print("recorte", trim.shape, "-> Q8_0", trim_q8.shape, trim_q8.dtype)

r_u = GGUFReader(unsloth)
arch = r_u.fields["general.architecture"].parts[-1].tobytes().decode()
w = GGUFWriter(salida, arch)
# metadatos: copiar todos menos los que GGUFWriter pone solo
for name, f in r_u.fields.items():
    if name in ("GGUF.version", "GGUF.tensor_count", "GGUF.kv_count", "general.architecture"):
        continue
    # copia generica por tipo
    ft = f.types
    if not ft:
        continue
    if ft[0] == 9:  # array
        sub = ft[1]
        if sub == 8:
            w.add_array(name, [f.parts[i].tobytes().decode() for i in f.data])
        else:
            w.add_array(name, [f.parts[i].tolist()[0] for i in f.data])
        continue
    val = f.parts[-1]
    t0 = ft[0]
    if t0 == 8:
        w.add_string(name, val.tobytes().decode())
    elif t0 == 7:
        w.add_bool(name, bool(val[0]))
    elif t0 in (0, 2, 4, 10):
        {0: w.add_uint8, 2: w.add_uint16, 4: w.add_uint32, 10: w.add_uint64}[t0](name, int(val[0]))
    elif t0 in (1, 3, 5, 11):
        {1: w.add_int8, 3: w.add_int16, 5: w.add_int32, 11: w.add_int64}[t0](name, int(val[0]))
    elif t0 == 6:
        w.add_float32(name, float(val[0]))
    elif t0 == 12:
        w.add_float64(name, float(val[0]))
w.add_string("general.frspec.note",
             "output.weight = 65536 frequency-ranked rows of the target output.weight "
             "(d2t ranking from drluoto/Qwen3.8-Flash-Next-MTP-GGUF), cut from the Unsloth head")
for t in r_u.tensors:
    assert t.name != "output.weight"
    # GGUFWriter espera la forma EN BYTES (la de t.data) para los cuantizados y
    # la deshace el solo con quant_shape_from_byte_shape.
    w.add_tensor(t.name, np.array(t.data), raw_dtype=t.tensor_type)
w.add_tensor("output.weight", trim_q8, raw_dtype=GGMLQuantizationType.Q8_0)
w.add_tensor("d2t", d2t, raw_dtype=GGMLQuantizationType.I64)
w.write_header_to_file(); w.write_kv_data_to_file(); w.write_tensors_to_file(); w.close()
print("escrito", salida, os.path.getsize(salida) / 1e9, "GB")
