#!/usr/bin/env python3
"""Porta a nuestra base (df03399 + PR28501 + PR28243) el recorte FR-Spec de la
cabeza MTP de drluoto: `output.weight` de 65 536 filas + `d2t` (I64) que mapea
fila del borrador -> id del vocabulario completo. Sus 6 parches no aplican
(su qwen4exp.cpp parte de otro punto); esto reescribe los DOS cambios que
importan sobre el fichero nuestro:

  1. load_arch_tensors: si el fichero es solo-MTP y trae `d2t`, crear `output`
     con n_vocab_out = ne[0] de d2t, y cargar `d2t`.
  2. graph_mtp: tras el matmul de la cabeza, si model.d2t existe, esparcir los
     logits recortados a un tensor [n_vocab_full, n_out] relleno de -inf con
     ggml_set_rows. El tronco verifica sobre el vocabulario completo, asi que
     el texto no cambia; solo el matmul del borrador es 3,8x mas pequeno.

Uso: portar-frspec.py <qwen4exp.cpp>   (edita in situ; idempotente)
"""
import re
import sys

p = sys.argv[1]
s = open(p, encoding="utf-8").read()
if "d2t draft-vocab trim" in s:
    print("ya portado"); sys.exit(0)

# --- 1. carga --------------------------------------------------------------
old1 = '''    output = create_tensor(tn(LLM_TENSOR_OUTPUT, "weight"), { n_embd, n_vocab }, TENSOR_NOT_REQUIRED);
    // tie_word_embeddings is false here: never tie to a token_embd a borrowing draft lacks.
    if (output == NULL && tok_embd != NULL) {
        output = create_tensor(tn(LLM_TENSOR_TOKEN_EMBD, "weight"), { n_embd, n_vocab }, TENSOR_DUPLICATED);
    }'''
new1 = '''    // FR-Spec d2t draft-vocab trim (H-036, port of drluoto/avifenesh): an MTP-only
    // sidecar may carry `output.weight` over a frequency-ranked subset of the vocab
    // plus a `d2t` map back to full ids. Real trunks never have d2t: no-op for them.
    int64_t n_vocab_out = n_vocab;
    if (mtp_only && ml.get_tensor_meta("d2t") != nullptr) {
        n_vocab_out = ml.get_tensor_meta("d2t")->ne[0];
        d2t = create_tensor(tn(LLM_TENSOR_D2T), { n_vocab_out }, 0);
        LLAMA_LOG_INFO("%s: QWEN4EXP MTP using d2t draft-vocab trim (n_vocab_out = %lld)\\n",
                       __func__, (long long) n_vocab_out);
    }

    output = create_tensor(tn(LLM_TENSOR_OUTPUT, "weight"), { n_embd, n_vocab_out }, TENSOR_NOT_REQUIRED);
    // tie_word_embeddings is false here: never tie to a token_embd a borrowing draft lacks.
    if (output == NULL && tok_embd != NULL) {
        GGML_ASSERT(!d2t && "d2t draft-vocab trim requires its own output.weight");
        output = create_tensor(tn(LLM_TENSOR_TOKEN_EMBD, "weight"), { n_embd, n_vocab }, TENSOR_DUPLICATED);
    }'''
assert s.count(old1) == 1, "ancla 1 no encontrada"
s = s.replace(old1, new1)

# --- 2. grafo MTP: esparcir logits -------------------------------------------
old2 = '''    cur = build_lora_mm(head_w, cur, head_s);
    cb(cur, "result_output", -1);
    res->t_logits = cur;

    ggml_build_forward_expand(gf, cur);
}'''
new2 = '''    cur = build_lora_mm(head_w, cur, head_s);

    if (model.d2t) {
        // FR-Spec: scatter the trimmed logits into full-vocab shape (rest = -inf) so
        // sampling/verification never learn the draft scored only a subset.
        const int64_t n_draft_vocab = cur->ne[0];
        const int64_t n_outputs     = cur->ne[1];
        const int64_t n_vocab_full  = (int64_t) model.vocab.n_tokens();
        GGML_ASSERT(model.d2t->type == GGML_TYPE_I64 || model.d2t->type == GGML_TYPE_I32);
        GGML_ASSERT(model.d2t->ne[0] == n_draft_vocab);
        ggml_tensor * full = ggml_fill(ctx0, ggml_new_tensor_3d(ctx0, GGML_TYPE_F32, 1, n_vocab_full, n_outputs), -INFINITY);
        cur = ggml_set_rows(ctx0, full,
                ggml_reshape_3d(ctx0, cur,       1,             n_draft_vocab, n_outputs),
                ggml_reshape_3d(ctx0, model.d2t, n_draft_vocab, 1,             1));
        cur = ggml_reshape_2d(ctx0, cur, n_vocab_full, n_outputs);
        cb(cur, "result_output_d2t", -1);
    }

    cb(cur, "result_output", -1);
    res->t_logits = cur;

    ggml_build_forward_expand(gf, cur);
}'''
assert s.count(old2) == 1, "ancla 2 no encontrada"
s = s.replace(old2, new2)

if "#include <cmath>" not in s and "INFINITY" in s:
    s = s.replace("#include", "#include <cmath>\n#include", 1)
open(p, "w", encoding="utf-8").write(s)
print("portado")
