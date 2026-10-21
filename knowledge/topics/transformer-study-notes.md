---
title: Transformer Study Notes (from zero to complete)
type: topic
created: '2026-10-19'
updated: '2026-10-19'
source_ids:
- doubao:transformer-study-notes
provenance_status: legacy_incomplete
---

# Transformer Study Notes (from zero to complete)

A structured Q&A study guide built from an individual's complete learning journey through the Transformer architecture — from tokenization to backpropagation. Each section starts with a one-line quick answer, followed by a full explanation with formulas, worked numeric examples, and comparison tables.

## How to use this document

This is **not a self-contained introduction to the Transformer**. It is a companion notebook to read alongside the standard reference (the original "Attention Is All You Need" paper, LLaMA/DeepSeek docs, or a full textbook). The purpose is to surface the points that are usually glossed over or easy to get wrong — the exact spots where a first-time reader gets stuck — and explain them plainly, with worked numbers. Skim a chapter when you hit that concept in the main reference and find it unclear.

**What each section covers:**

- **01–02 Token & Embedding** — what a token is, how it becomes an integer ID and then a float vector; numeric precision (BF16/FP8) and memory cost.
- **03 Positional encoding & vector math** — why order matters, learnable vs. sinusoidal vs. RoPE; dot product, linear vs. nonlinear ops.
- **04–05 QKV & multi-head attention** — what Q/K/V really are, weighted sum vs. "picking one", K/V binding, dynamic vs. fixed weights, W_O, head splitting.
- **06–07 LayerNorm & MLP** — what normalization does geometrically, where the nonlinearity lives, activation functions.
- **08–09 Residuals & stacking** — shortcut paths, fork vs. merge gradients, why deep networks train at all, layer ordering.
- **10 Training & scaling** — gradient descent vs. optimizers, Teacher Forcing, masking, EOS/padding, Chinchilla scaling laws.
- **11 Inference (Prefill/Decode/KV Cache)** — the two generation phases, what gets cached and why it speeds up decode.
- **12 Backpropagation & loss** — chain rule from the loss down to parameters, softmax+cross-entropy gradient, lm_head.
- **13 Reflections** — emergent ability, distillation, the "point cloud" view of embeddings.
- **14 MoE** — supplement on sparse mixture-of-experts (dense vs. activated params).
- **15 Quick reference** — formula tables, comparison tables, common pitfalls, core numeric examples.

## 01 Token (the entry point)

### Question: What is a token?

> **In one line**: A token is a unit produced by the tokenizer when it splits text; it is the basic unit that the input sequence is processed token-by-token.

A model cannot read raw text directly; it must first split the text into small units, and each unit is called a token, produced by a tokenizer. The granularity is not fixed:

- English long words are split into subwords:
  ```
  "unbelievable" → "un" + "believ" + "able"
  ```
- A single Chinese character or word can also map to one or more tokens.

A sentence becomes a sequence of tokens when it enters the model:

```
input text → tokenizer → [token₀, token₁, token₂, …, tokenₙ₋₁]
```

So "the model processing text" is essentially processing this sequence of tokens one by one; the token is the smallest unit the model works with.

### Question: What does a token correspond to inside the model?

> **In one line**: Each token has a unique integer index in the vocabulary; text → tokenization → a sequence of integer IDs is the raw input to the model.

The tokenizer maintains a "token ↔ index" lookup table, called the vocabulary, where each token has a unique integer ID. The vocabulary is large: roughly 50,000 for GPT-2, and tens of thousands to hundreds of thousands for modern models. When a sentence enters the model, it is first tokenized and mapped into a sequence of integer IDs, which is the model's raw input:

```
"un" → 3020, "believ" → 4382, "able" → 573
(example IDs, not real values)
```

The embedding stage then uses these IDs to look up vectors (see §02). Think of the token ID as the "entry index" from text into numeric space — a bridge between text and vectors.

### Question: Why can token vectors be added together (each dimension is a feature)?

> **In one line**: Each dimension is an independent feature axis, so vector addition is just stacking features axis-by-axis, which makes sense semantically.

The key is to understand "what each dimension of a vector actually is". A d-dimensional embedding can be viewed as projecting a word onto d "feature axes", where each axis records how strong the word is on that feature:

```
word → [feature₀, feature₁, feature₂, …, featured₋₁]
            ↑       ↑       ↑
          animal  emotion  motion   … (example, illustrative only)
```

Because they are independent feature axes, adding two vectors means stacking feature values axis-by-axis, i.e., merging the features of two words. For example, on the "animal" axis, adding the vectors of "cat" and "dog" makes the animal-feature response stronger than either alone:

```
[cat]      = [2, 0, 1, …]   (animal axis = 2)
[dog]      = [1, 0, 3, …]   (animal axis = 1)
[cat]+[dog] = [3, 0, 4, …]   (animal axis = 3, more prominent)
```

Because each dimension is an independent feature axis, axis-by-axis stacking is semantically sound — and this is exactly why the attention weighted mixture of V and the direct addition in residual connections can work.

## 02 Embedding and Numeric Precision

### Question: Is an embedding an integer or a float? How many bits? Is it F16?

> **In one line**: Every element of an embedding vector is a float (not an integer, not a probability); precision is set by the storage format; training mainstream is BF16, inference is often quantized to FP8/INT8.

An embedding is a vector looked up by token ID (see §01), and every element is a **float**. GPT-2's embedding vectors have 768 elements, all floats. Crucially, these numbers are **not integers**, and they have **no 0–1 bound and are not probabilities** — they typically sit near [-1, +1] but can be larger or smaller, learned entirely by training (e.g., 0.3412, -0.713, 0.052).

"How many bits" depends on the storage format:

| Type | Bits | Bytes | Use |
|---|---|---|---|
| FP32 | 32-bit | 4 bytes | Single precision, original weight storage |
| FP16 | 16-bit | 2 bytes | Half precision, early training |
| BF16 | 16-bit | 2 bytes | **Mainstream for LLM training**, exponent range matches FP32 |
| FP8 | 8-bit | 1 byte | Inference quantization, native in new open-source |
| INT8 | 8-bit | 1 byte | Integer quantization |
| INT4 | 4-bit | 0.5 byte | More aggressive quantization |

So the answer is: **embedding is a float**, and the precision depends on format — GPT-2's raw weights are saved as float32, and local inference usually converts to FP16/BF16.

### Question: What float sizes do GPT / Claude / DeepSeek use now?

> **In one line**: Training mainstream is BF16; native inference baseline is BF16/FP8; large-scale online serving is quantized to FP8/INT8/FP4.

| Model | Training | Inference |
|---|---|---|
| GPT-2 (open) | FP32 saved | converts to FP16/BF16 |
| GPT-3/3.5/4 (closed) | BF16 mixed precision | mostly quantized FP8 |
| Claude (closed) | BF16 | FP8 (common for KV Cache), core layers keep BF16 |
| DeepSeek-V3 | **native FP8** | FP8 / BF16 |
| DeepSeek-V4 | MoE expert layers FP4 | other layers FP8 |

Scenarios (easy to confuse):

- **Pretraining**: BF16 mixed precision (parameter updates in FP32, forward/backward in BF16); DeepSeek uses FP8 directly.
- **Native weight checkpoints**: older open-source BF16; new open-source (DeepSeek-V3) FP8.
- **Online inference API**: quantize BF16→FP8, MoE expert layers even FP4.
- **Local deployment (llama.cpp)**: can go further to INT8/INT4.

**BF16 vs FP16**: FP16 has 10-bit mantissa, 5-bit exponent, small dynamic range, prone to overflow; BF16 has 7-bit mantissa, 8-bit exponent, **exponent range identical to FP32**, so it's the first choice for LLM training.

### Question: How much space does each token's vector take?

> **In one line**: A 768-dim vector is about 3KB/token in FP32 and 1.5KB/token in BF16/FP16; this 768 is d_model (hidden dimension).

Back to GPT-2: each token's X vector has 768 floats:

- FP32: 768 × 4 = 3072 bytes ≈ 3KB / token
- BF16/FP16: 768 × 2 = 1536 bytes ≈ 1.5KB / token

This 768 is d_model (hidden dimension), i.e., "how many dimensions one token's vector has". LLaMA uses 4096, 5120, 8192, etc.

## 03 Positional Encoding and Vector Operations

### Question: Does the input need positional encoding added after tokenization?

> **In one line**: Yes. The final input X = token embedding + positional encoding (vector addition, not concatenation), and X is then used to compute Q/K/V.

GPT-2's pipeline:

1. The tokenizer splits text into tokens;
2. Each token looks up the word-embedding table → 768-dim vector;
3. **Add the positional encoding** → the final X_j;
4. X_j is then multiplied by W_Q/W_K/W_V to compute Q/K/V.

Formula:

$$
X_j = \text{TokenEmbedding}(token_j) + \text{PosEmbedding}(pos_j)
$$

⚠️ It is **vector addition**, not concatenation — both are 768-dim. Why can they be added? Because each dimension is an independent feature axis, and addition is feature stacking (see §01).

### Question: Why is positional information necessary?

> **In one line**: Self-attention is order-insensitive by itself; without positional encoding, the model treats `cat chases dog` and `dog chases cat` as nearly identical.

Self-attention only does "who is related to whom" weighting and does not care about order. Without injecting positional information, the model cannot distinguish sequence order like "cat chases dog" vs "dog chases cat". Positional encoding tags each token with "where am I in the sequence".

### Question: What are the differences among the three positional encoding schemes?

> **In one line**: GPT series use a learnable position table (added to X); the original paper uses sin/cos formulas computed on the fly; LLaMA/DeepSeek use RoPE that rotates Q/K (not V).

| Scheme | Approach | Usage |
|---|---|---|
| Learnable position embedding (GPT-1/GPT-2) | Position vectors are model parameters learned by training; table shape [max_ctx, d_model], GPT-2 is 1024×768 | Position added onto X |
| Sinusoidal positional encoding (original paper) | No learning, computed on the fly with sin/cos formulas, supports longer sequences | Not used by GPT |
| RoPE rotary positional encoding (LLaMA/DeepSeek/GPT-3.5) | **Not added to X**; rotates Q/K after projection, before the dot product | Position enters Q/K, not V |

### Question: Why does the output stay the same when a leading space is added?

> **In one line**: A space yields a different token ID, so mathematically all of QKV changes, but the model learns the space is "meaningless whitespace" and gives it very low attention weight, so most of the time the output is unchanged.

- A space is tokenized as a separate token or attached to the preceding word (`▁hello` vs `hello` are **two different tokens**);
- A leading space → different token ID → different embedding → shifted positions → **mathematically all QKV changes**;
- But most of the time the output is unchanged: **the model learns the space is "meaningless whitespace" and gives it very low attention weight**, so it barely affects the rest.

⚠️ Not always identical: on short prompts / sensitive instructions, adding a space can change the result (RoPE models shift all positions, so changes are more likely).

### Question: How does the model "filter out" meaningless words?

> **In one line**: There is no filter module; it relies on attention-weight allocation to suppress a token's contribution to nearly nothing.

`Out_i = Σ_j A[i,j]·V_j` — if token j is a meaningless word:

- K_j has low similarity to useful tokens' Q_i;
- after softmax, A[i,j] weight is near 0;
- the A[i,j]·V_j term is ≈ 0, so its contribution to the output disappears.

> It does not delete the token; it compresses its contribution to nearly nothing during the weighted sum. This is a statistical result fitted from high-dimensional vector space + massive data, not an explicit judgment logic.

⚠️ There are limits: stuffing a long context with filler fails; RoPE insertion changes position offsets; too many filler words dilute the weights.

### Question: What is the geometric meaning of the dot product (inner product)?

> **In one line**: The dot product of two vectors measures how close their directions are; the closer the directions, the larger the dot product.

$$
a \cdot b = |a||b|\cos\theta
$$

- θ=0°: cosθ=1 → large positive
- θ=90°: cosθ=0 → 0
- θ=180°: cosθ=-1 → negative

In attention: score_ij = Q_i·K_jᵀ, a large score = highly related, 0 = unrelated, negative = negatively related.

**Why divide by √d_k?** The higher the vector dimension, the larger the dot product naturally grows and the more its variance inflates, making softmax extremely sharp. Dividing by √d_k stabilizes the distribution.

> The dot product is **not distance**! Distance measures how far apart; the dot product measures how similar the directions are and is also affected by the norms.

### Question: Can the dot product be negative?

> **In one line**: Yes. The elements of Q/K are themselves floats that can be positive or negative, so the dot product can be negative; but after softmax the weights are always ≥0 and each row sums to 1.

- The elements of Q/K vectors are floats, **positive or negative**;
- So the dot product result **can absolutely be negative**;
- Example: Q_i=[0.5,0,0], K_j=[-0.5,0,0], dot = -0.25;
- But **after softmax the weights are always ≥0 and each row sums to 1** (eˣ is always positive).

> Distinguish: raw QK dot-product scores can be positive/zero/negative; attention weights after softmax are always positive.

### Question: Why does attention only use linear operations? Where do other operations live?

> **In one line**: The Q/K/V projections and the final weighted sum of V are linear; softmax inside attention is itself a nonlinear operation, and the MLP adds another nonlinearity via its activation function.

The attention layer is mostly linear: Q/K/V projections, the dot-product scores QKᵀ, and the final weighted sum of V are all linear operations (fast on GPU, easy to differentiate, inner product naturally measures direction similarity). **However, softmax is itself nonlinear** — it applies an exponential and a normalization. So attention is not "all linear"; it has a softmax nonlinearity in the middle. The later MLP adds a second nonlinearity through its activation functions.

| Operation | Output | Use |
|---|---|---|
| Dot product (inner product) | scalar | QK scoring |
| Matrix multiplication | vector | generating QKV |
| Vector add/subtract | vector | embedding + positional encoding |
| Weighted sum of V | vector | attention output (linear in V) |
| Hadamard product (element-wise multiply) | vector | SwiGLU gating |
| concat | longer vector | multi-head merge |
| exponential / softmax | vector | attention weights (nonlinear) |
| activation (ReLU / SwiGLU) | vector | MLP hidden layer (nonlinear) |

**Why keep the linear parts linear?** Linear ops are parallel-fast on GPU, easy to differentiate, and the inner product naturally measures direction similarity. **Where is the nonlinearity?** In two places: (1) softmax inside attention, which turns raw scores into normalized weights; (2) the MLP's activation functions (ReLU/SwiGLU), which extract complex features. **Without any nonlinearity, stacking layers is equivalent to a single matrix multiplication, making the model very weak.**

> In one line: Attention (linear QK scoring + weighted V, but with a softmax nonlinearity in between) + MLP (nonlinear activation, extracts complex features) work together.

> **Note**: The linear part to pay attention to is the final weighted sum of V — `Out = Σ weight_i · V_i` is a linear combination. But the weights themselves come from softmax, which is nonlinear; and the QK dot-product scores are then fed through softmax before they reach V. So the only truly linear step in attention is the weighted sum of V given fixed weights.

## 04 QKV Attention Mechanism

### Question: What are Q, K, and V? After the dot product, does V belong to K or to Q?

> **In one line**: Q, K, V are three independently projected sets of vectors that only share the same source; V belongs to neither K nor Q.

Q, K, V are each produced independently. The same input X, multiplied by three different sets of trainable weights, gives three sets of vectors:

```
Q = X·W_Q
K = X·W_K
V = X·W_V
```

They share the same source (all from X), but each plays a different role:

| Letter | Meaning | Analogy (database search) |
|---|---|---|
| Q (Query) | What the current token wants to retrieve | search term |
| K (Key) | The retrieval label each token exposes | index label |
| V (Value) | The actual content carried | document body |

Attention uses Q to compare against K, decides "who to look at", then retrieves the corresponding V as the actual content. So V belongs to neither K nor Q; it merely shares source X with them.

### Question: How does QK "extract" V? Does it pick one out?

> **In one line**: It is not extraction but a weighted sum; it mixes all V by similarity instead of selecting one.

There is a common misconception that attention "picks out" the single most relevant V. That is wrong. The full computation is:

$$
\text{Attention}(Q,K,V) = \text{softmax}\left(\frac{QK^\top}{\sqrt{d_k}}\right)V
$$

in three steps:

1. Q dot-products with all K to get each token's similarity score;
2. softmax normalizes each row into attention weights;
3. the weight matrix multiplies V to take a weighted average of all V at each position.

Each position's output is a mixture of all V weighted by their attention — the larger the weight, the larger the contribution; V with near-zero weight barely affects the result. A concrete 3-dim numeric example:

```
Let Q = K = I (identity), V = diag(10, 20, 30)
Then QKᵀ = identity, scores for row 0 = [1, 0, 0]
softmax([1, 0, 0]) = [e, 1, 1] / (e + 2) ≈ [0.576, 0.212, 0.212]
→ Out₀ = 0.576·10 + 0.212·20 + 0.212·30 ≈ 16.4
```

Note: softmax does **not** preserve the identity matrix — even when the raw score is [1,0,0], the weights are [0.576, 0.212, 0.212], not [1,0,0]. The diagonal just means the token's own V gets the largest weight; the other two still contribute.

Change K to make attention "tilt":

```
If Q₀ dot-products with the three K to scores [1, 1, 0]
→ softmax ≈ [0.422, 0.422, 0.155]
→ Out₀ = 0.422·V₀ + 0.422·V₁ + 0.155·V₂
```

So the output is "mixed by weight" rather than "picked one". It can be a weighted sum because each dimension of V is a feature (see §01); the weighted mixture is essentially feature fusion.

### Question: Are Kj and Vj bound together?

> **In one line**: Yes. A[i,j] corresponds to V_j, j is always the same index; the X_j at position j times the three weight matrices gives Q_j/K_j/V_j, so K_j and V_j are co-originated and paired.

Yes. The attention weight A[i,j] means "how much position i should attend to position j", and the value it takes is always V_j — the index never shifts. The reason: for the same token (position j), we take its own embedding X_j and multiply it by the three weight matrices to get three vectors at once:

```
X_j · W_Q → Q_j
X_j · W_K → K_j
X_j · W_V → V_j
```

So K_j and V_j are naturally a co-originated pair — V_j is simply X_j after the linear transform W_V. They are bound from the start and never drift apart.

### Question: Are QKV fixed or computed dynamically each time?

> **In one line**: W_Q/W_K/W_V are fixed after training and shared globally; Q_j/K_j/V_j are computed on the fly by multiplying the fixed W by the current input X_j; the model does not pre-store QKV.

You must distinguish two layers: **weights** and **vectors**.

| Object | What it is | Fixed? |
|---|---|---|
| Weights W_Q/W_K/W_V | model parameters | fixed after training, shared by all tokens |
| Q, K, V | intermediate vectors from fixed W × current input | change with input, computed dynamically |

There is no such thing as "the model stores a bunch of QKV" — at inference, given an input sequence, it projects out this instance of QKV on the fly. Training adjusts the three W matrices, not QKV itself; QKV is just a dynamically produced result of each forward pass.

### Question: What is the difference between matrix multiplication and dot product?

> **In one line**: Matrix multiplication X·W_Q takes vector × matrix and outputs a new vector, used to linearly map the embedding to produce Q/K/V; the dot product Q_i·K_jᵀ takes two vectors and outputs a scalar (similarity score), used for scoring.

They differ in input, output, and role:

| | Matrix multiplication X·W_Q | Dot product Q_i·K_jᵀ |
|---|---|---|
| Input | vector × matrix | two vectors |
| Output | a new vector | a scalar (similarity score) |
| Role | linear mapping, generates Q/K/V | similarity scoring |

Matrix multiplication performs a linear transform (rotation, stretch, shift) on the embedding, mapping it to another vector space; written in batch form Q = X·W_Q, it computes all tokens' Q at once, which is how GPU gets its parallelism. The dot product, by contrast, multiplies two vectors element-wise and sums them to a scalar, measuring how similar the two vectors' directions are (see §03). In short: matrix multiplication "generates", the dot product "scores".

## 05 Multi-Head Attention (with W_O)

### Question: What is multi-head attention (MHA)?

> **In one line**: Instead of using one set of Q/K/V for attention, it splits Q, K, V into multiple independent "heads", each does its own attention, then concatenates and passes through one more linear projection.

The core idea: let different attention heads learn different kinds of relationships (syntactic dependencies, long-range coreference, local adjacency).

Full steps (assume d_model=4096, heads h=32, per-head d_k=128):

1. Input vector × W_Q/W_K/W_V → complete Q, K, V ∈ [T, 4096];
2. **Split heads**: cut along the feature dimension into h parts, Q:[T,4096]→[T,32,128] (each head gets its own 128-dim q/k/v);
3. **Each head computes attention independently**: head_i = softmax(Q_iK_iᵀ/√d_k)·V_i (prefill with causal mask);
4. **Concat all heads' outputs**: 32×128=4096, restoring [T,4096];
5. **Output projection W_O**: concatenated result × W_O (linear layer) → attn_output.

### Question: What does W_O do? Is it easy to forget?

> **In one line**: After concatenating the heads, there must be one more linear projection W_O; it is the last step of the multi-head attention module and fuses the heads' results back into a single output.

- W_O fuses the 32 heads' concatenated result back into a single output;
- It differs from the three W matrices in §04: those generate QKV; this one fuses multi-head outputs.

Intuition: single-head = one person with one view; multi-head = 32 independent observers each judging word relationships by their own standard, then fusing everything after they finish.

### Question: Does each head have its own WQ/WK/WV? Does it run 32 projections?

> **In one line**: No. Standard MHA uses one set of W, generates complete QKV, then splits by reshape; it barely adds computation.

**Key details**:

- Splitting is just reshape, **not separate WQ/WK/WV per head** (standard MHA uses one set of W, generating complete QKV then splitting);
- MQA/GQA (Llama 2/3) are the ones that share K/V, to reduce KV-cache memory;
- The causal mask is the same triangle mask shared by all heads.

**Two misconceptions**:

- ❌ Each head has independent WQ/WK/WV → ✅ one set of W, generate complete QKV then split
- ❌ Multi-head runs QKV projection 32 separate times → ✅ projection runs once, splitting is reshape, barely adds computation

### Question: What is 4096? Is it the length of a single token after embedding?

> **In one line**: 4096 is d_model (hidden dimension), the dimensionality of one token's vector after embedding; it stays unchanged throughout the whole block.

This 4096 stays unchanged throughout the block (except the reshape in multi-head splitting):

- embedding output (T,4096)
- LN, WQ/WK/WV projections still (T,4096)
- reshape into 32 heads → (T,32,128), just reshaping, no numbers added/removed
- concat back to (T,4096), ×W_O still outputs (T,4096)
- residual, LN2, MLP input still 4096 (MLP expands internally then reduces back to 4096)

**Residual addition requires the two vectors to have identical dimensions**, so the block input/output must stay 4096.

**Two easy-to-confuse dimensions**:

- d_model=4096: the width of one token's main vector (embedding, block in/out)
- d_k=128: the Q/K/V vector dimension inside one head, d_k = d_model/num_heads

> LLaMA 7B: hidden=4096, 32 heads, d_k=128.

### Question: Are QKV shared across layers and heads?

> **In one line**: Each layer has its own QKV weights (not shared across layers); all tokens in the same head share that head's QKV, and heads are independent of each other.

- **Each layer has its own QKV weights** (WQ/WK/WV per layer, not shared across layers);
- all tokens in the same head share that head's QKV; heads are independent;
- QKV is computed on the fly by multiplying the fixed W by the current input X_j.

## 06 LayerNorm

### Question: What is the mathematical motivation for LayerNorm?

> **In one line**: Stacking many Transformer layers makes vector magnitudes grow or shrink toward 0, causing gradient explosion/vanishing; LN standardizes each vector to mean 0, variance 1.

- Stacking many Transformer layers makes vector magnitudes grow ever larger or approach 0, causing gradient explosion/vanishing;
- LN "standardizes" each vector: mean to 0, variance to 1 (with a small learnable gain/bias);
- It normalizes each token's vector independently — it does not preserve angles or distances between tokens.

### Question: What is the plain-language explanation of LayerNorm?

> **In one line**: For each token's vector independently, shift its center to the origin and rescale to unit variance — a per-token standardization that stabilizes the numeric range, not a rigid geometric transform.

Think of each token's vector as a point in high-dimensional space. LN takes each point independently, subtracts its own mean (moves its center to the origin), and divides by its own standard deviation (rescales). The point cloud's overall shape does change — relative angles and distances between tokens are not preserved, because each vector is normalized on its own. The purpose is to keep every vector in a similar numeric range so that deep stacking trains stably.

> **Note**: The "point cloud" here is a loose analogy for intuition only. Strictly speaking, LN applied independently per token does distort the shape of the point cloud — angles and distances between tokens are not preserved. The analogy helps build intuition about "moving each vector to a standard position", but it should not be read as a rigid geometric transform.

### Question: Why can vectors still be added / residuals used after LN?

> **In one line**: Because LN outputs are still well-scaled vectors in the same dimension; residual addition requires matching dimensions, not preserved angles — the learnable gain/bias lets the model re-calibrate after normalization.

Normalization changes each vector's internal ratios, but it outputs a vector of the same dimension. Residual addition only requires two vectors to have the same shape; it does not require angles to be preserved. The learnable gain and bias let the model adjust back after normalization, so residual/addition after LN still works.

### Question: What is the mathematical form of LN?

> **In one line**: Compute mean μ and variance σ² of the vector, do (x−μ)/√(σ²+ε), then multiply by gain and add bias.

For a vector x ∈ R^d:

1. Compute mean μ, variance σ²;
2. x_norm = (x − μ) / √(σ² + ε);
3. then × gain + bias (learnable).

### Question: Are residual connections and LayerNorm the same thing?

> **In one line**: No. Residual is direct vector addition (no parameters, prevents gradient vanishing); LN is mean-0 variance-1 (has gain/bias, stabilizes values).

| | Residual connection | LayerNorm |
|---|---|---|
| Operation | direct vector addition | mean-0 variance-1 |
| Parameters | none | gain/bias |
| Role | shortcut, prevents gradient vanishing | stabilizes values/gradients |

## 07 MLP (Feed-Forward Network)

### Question: Where exactly is the nonlinearity?

> **In one line**: There are two nonlinearities — softmax inside attention, and the activation function inside the MLP. The linear parts (Q/K/V projections, weighted sum of V) stay linear.

The attention layer has two parts: the QK dot-product scores and the weighted sum of V are linear, but **softmax itself is nonlinear** (exponential + normalization). The MLP adds a second nonlinearity through its activation functions (ReLU / SwiGLU).

> Without any nonlinearity, stacking any number of linear layers is equivalent to a single matrix multiplication, making the model very weak.

### Question: Where does the MLP sit in the Transformer block?

> **In one line**: After attention (Pre-LN structure): residual → LN2 → MLP → residual.

In a single layer, MLP comes after attention:

```
x_attn (attention residual output)
  → LN2 → MLP → mlp_out
  → residual 2: x_out = x_attn + mlp_out
```

### Question: What is the internal structure of the MLP?

> **In one line**: Up-projection → activation → down-projection, i.e., "expand then compress" nonlinear feature extraction.

```
input x ∈ [T, d_model]
  → up-projection × W_up → [T, 4×d_model] (or higher)
  → activation (ReLU / GeLU / SwiGLU)
  → down-projection × W_down → [T, d_model]
```

- Expand first (usually 4×) then reduce back to d_model, a "expand-then-compress" nonlinear feature extraction;
- Most parameters live in the MLP (about 2/3 in MLP, 1/3 in Attention).

### Question: What common activation functions are there, and how do they differ?

> **In one line**: ReLU is simple but can have dead neurons; GeLU is a smooth ReLU (common in Transformers); SwiGLU uses gating (LLaMA/DeepSeek).

| Activation | Trait |
|---|---|
| ReLU | max(0,x), simple; gradient 0 for x<0 (sparse but possibly "dead neurons") |
| GeLU | smooth ReLU, common in Transformers |
| SwiGLU | gated: sigmoid gate × linear transform, used by LLaMA/DeepSeek; uses Hadamard product |

**What about ReLU being non-differentiable (subgradient, see §12)**:

- ReLU is non-differentiable at x=0, but we take an agreed value (x>0→1, x<0→0, x=0→usually 0);
- Since the probability of x being exactly 0 is 0 (continuous distribution), training practically never hits it, and the effect is negligible.

### Question: How does the MLP differ in MoE models?

> **In one line**: In dense models the MLP is a single feed-forward; in MoE models the MLP layer is replaced by multiple experts selected by a router (top-k); Attention (QKV) is always dense, only MLP is sparse.

- Dense model: MLP is a single feed-forward layer;
- MoE model: **the MLP layer is replaced by multiple experts (MoE)**, the router selects top-k experts;
- Attention (QKV) is always dense; only MLP is sparse (see §14).
## 08 Residual Connections

### Question: How many residual connections are there per Transformer layer?

> **In one line**: Two per layer — one around the attention, one around the MLP.

In one layer: the first residual wraps attention (add the pre-LN raw x), the second wraps the MLP. Both are "shortcut: x + f(x)".

### Question: What is the exact path of the residual connection?

> **In one line**: shortcut x flows directly and is added to the output of the module (attention or MLP) that is the input to that module; the added x is the pre-LN raw x, not the LN output.

Let me show the residual paths for one layer (taking the two sublayers as an example):

```
x_in
  └─ shortcut1: x_in ──────────────┐
  └─ LN1 → attention → attn_out ───┤
                       x_attn = x_in + attn_out
  └─ shortcut2: x_attn ──────────────┐
  └─ LN2 → MLP → mlp_out ────────────┤
                       x_out = x_attn + mlp_out
```

Key details:

- The added x is the **pre-LN raw x**, not the LN output;
- Both x and the module output must have identical dimensions (d_model);
- If dimensions differ across layers, a projection (W_res) is needed;
- Two residuals per layer → with N layers, there are 2N residuals total.

### Question: What problem does the residual solve?

> **In one line**: Gradient vanishing in deep networks. Directly adding the shortcut gives the backprop path a constant "1" term, so deep layers' gradients propagate stably.

For a block y = x + f(x):

$$
\frac{\partial L}{\partial x} = \frac{\partial L}{\partial y}\left(1+\frac{\partial f}{\partial x}\right)
$$

The shortcut contributes the constant "1" — even if ∂f/∂x is small, the gradient has a direct route to the earlier layers. Without the residual, gradient magnitude ∝ product of many small ∂f/∂x → vanishes quickly. The residual turns the product into a sum.

### Question: In backprop, at the residual fork, do the two paths each propagate their own gradient?

> **In one line**: Yes. Because the residual is addition, the two branches each get the upstream gradient and propagate it separately; the sum of the two gradients equals the upstream gradient at the outer layer.

Because y = x + f(x) is addition. Backprop for x:

$$
\frac{\partial L}{\partial x} = \frac{\partial L}{\partial y}\left(1+\frac{\partial f}{\partial x}\right)
= \frac{\partial L}{\partial y}\cdot 1 + \frac{\partial L}{\partial y}\cdot \frac{\partial f}{\partial x}
$$

Split:

- **shortcut branch**: ∂L/∂y × 1 (direct, upstream gradient passed straight back to x);
- **residual-function branch**: ∂L/∂y × ∂f/∂x (goes into f, then continues computing attention/MLP gradients).

At the fork (an addition node) it "splits flow": each branch gets the upstream gradient and propagates it on its own. The "1" of the shortcut is the reason gradient vanishing is prevented.

### Question: Why are the gradients equal at the merge point and added at the fork point?

> **In one line**: At an additive merge point (y=a+b), each input's gradient equals the upstream gradient (addition's derivative is 1); at a multi-way fork point, ∂L/∂x = sum of all branches' gradients. Both are pure partial differentiation/chain rule.

Two different nodes:

- **① Fork point (one input flows to multiple downstream)**: e.g., a vector simultaneously participates in attention and goes through the shortcut. In backprop this node receives one gradient from each downstream path and **sums them** as the upward gradient: ∂L/∂x = ∂L/∂y₁ + ∂L/∂y₂;
- **② Merge point (multiple inputs combine into one output via addition)**: e.g., x + attn_out. In backprop, the gradient of the output with respect to each input = upstream gradient × 1 (addition's derivative is 1), so **each input's gradient equals the upstream gradient (they are equal)**.

> In one line:
> - Additive merge point (y = a+b): ∂L/∂a = ∂L/∂b = ∂L/∂y (equal, because addition's derivative is 1)
> - Multi-way fork point (x into multiple branches): ∂L/∂x = sum of the branch gradients (because x affects all branches)
> This is the truth behind "merge points are equal, fork points add" — pure partial differentiation/chain rule.

### Question: Is this just partial-derivative theory, not something unique to neural networks?

> **In one line**: Correct. This is the partial differentiation and chain rule inherent to multivariate calculus, and backpropagation merely applies this ready-made rule automatically to the computation graph.

This theory is inherently the partial differentiation / chain rule of multivariate calculus — a mathematical conclusion that exists independent of neural networks; it is not a rule invented for backpropagation.

- **The math itself (calculus, unrelated to neural networks)**: as long as a variable x affects the final function L through two different paths, ∂L/∂x = contribution of path 1 + contribution of path 2. As long as y = a+b, ∂L/∂a = ∂L/∂y and ∂L/∂b = ∂L/∂y always hold. Whether it is a physical formula, an economic model, or a neural network, this always holds;
- **Backpropagation (engineering application)**: a neural network is just a big stack of nested functions (matrix multiplication, softmax, addition, activations, LayerNorm…). Backprop is the automatic traversal of this graph, applying the above calculus rules node by node to compute the partial derivatives of all parameters.

## 09 Multiple Layers and Stacking

### Question: Why do we stack so many layers? What do deeper layers do?

> **In one line**: More layers capture richer, higher-order abstractions and longer-range relationships; empirically "deeper generally improves" (with residual + LN to support very deep stacks).

Why so many layers:

1. **More layers capture richer, higher-order abstractions and longer-range relationships** (early layers grasp local grammar, middle layers syntax, deep layers semantic/coherence);
2. **Each layer re-relates the sequence**: attention relationships at every level, features progressively refined;
3. **Empirically "deeper generally improves"**: stacking layers increases capacity and expressiveness;
4. In theory, with residual connections and LayerNorm to support very deep stacks, the "too deep to train" problem is largely solved.

But "deeper is always better" is not absolute: the increase is diminishing, the cost rises linearly, and beyond a point the effect plateaus (see scaling laws §10).

### Question: How do layers communicate? What is the layer ordering?

> **In one line**: Layers are sequential — each layer's output is the next layer's input; within a layer, the fixed order is residual → LN1 → attention → residual → LN2 → MLP.

Layers are stacked sequentially: each layer's output is the next layer's input.

One layer (Pre-LN structure, LLaMA/DeepSeek):

```
x → residual → LN1 → attention → residual → LN2 → MLP → x'
```

(The residual runs around the module; for simplicity, LN order here follows Pre-LN: x → LN → attention → residual → LN → MLP → residual.)

At the very end of the last layer: the final hidden state is passed to lm_head to predict the next token (see §12).

### Question: Why does gradient stability matter for stacking layers?

> **In one line**: Deepening multiplies gradients across layers; residual provides a "1" path to keep gradients from vanishing, and LN stabilizes the value range — together they make deep stacking trainable.

In a network with many layers, backprop multiplies gradients layer by layer; without the residual's "1" path the gradients vanish. LN also stabilizes the value range. Together they make deep stacking trainable — this is why residual + LN is a structural prerequisite for building deep models.

### Question: What is the difference between LLaMA and GPT-2 in layer depth and stacking?

> **In one line**: GPT-2 is 12 layers, d_model 768; LLaMA 7B is 32 layers, d_model 4096; both are decoder-only stacks, differing mainly in scale, normalization style, activation, and position encoding.

| | GPT-2 | LLaMA 7B |
|---|---|---|
| layers | 12 | 32 |
| d_model | 768 | 4096 |
| heads | 12 | 32 |
| d_k | 64 | 128 |
| position encoding | learnable table | RoPE |
| activation | GeLU | SwiGLU |

## 10 Training and Scaling

### Question: What is the basic idea of gradient descent?

> **In one line**: Compute the gradient of the loss w.r.t. each parameter, then move each parameter a small step against the gradient direction (negative gradient), iterating to lower the loss.

Given a loss L, for each parameter θ:

1. Compute the gradient g = ∂L/∂θ (by backpropagation);
2. Update: θ ← θ − η·g (η is the learning rate).

Iterate many times to minimize the loss. Geometric meaning: the gradient points in the direction of the steepest increase; moving against it descends the loss fastest.

### Question: Is gradient descent "dividing by the slope"?

> **In one line**: No. The direction −g is a mathematical constraint; the step size proportional to the gradient (Δw=−ηg) is a chosen strategy, not a mathematical necessity.

Two different formulas — don't confuse them:

1. **Differential form ΔL ≈ g·Δw**: local analysis, how an infinitesimal change changes the loss;
2. **Update form Δw = −η·g**: descent strategy, walk in the negative-gradient direction.

**"Shouldn't we divide by the slope?"** → No. Clarify:

- **The direction −g** is a mathematical constraint (the negative-gradient direction is where the loss descends fastest — a property of the gradient);
- **The step size proportional to the gradient (Δw = −η·g)** is a **chosen strategy**, not a mathematical necessity. You can choose constant step, AdaGrad, Adam, etc.;
- Keep them apart: **direction is the constraint given by the gradient, step size is the strategy**. AdamW adapts the "step size" (momentum + per-parameter scaling).

> In one line: the gradient tells you where to go (direction), η and the optimizer tell you how far (step size). Δw=−ηg uses the strategy "bigger gradient, bigger step".

### Question: What is the classic optimizer story?

> **In one line**: Start with SGD (fixed learning rate), then Adam/AdamW add adaptive per-parameter step sizes and momentum; modern LLM training uses AdamW with cosine schedule and weight decay.

- **SGD**: θ ← θ − η·g, a single global learning rate;
- **Momentum**: accumulates a velocity, smooths the direction;
- **AdaGrad/RMSProp**: adapt the step size per parameter;
- **Adam/AdamW**: adaptive per-parameter step sizes + momentum (bias correction); AdamW decouples weight decay, stable and mainstream;
- Modern LLM training: AdamW + cosine learning-rate schedule + weight decay + gradient clipping + bf16 mixed precision (see §02).

### Question: What is Teacher Forcing? When training to predict T2, is the T1 used the model's prediction or the real T1?

> **In one line**: The real T1 (Teacher Forcing). During training each position directly uses the ground-truth real context, not the model's previous prediction; inference is when the model's own generated tokens are used.

This is the classic confusion point. **During training, the input used to predict T2 is not the model's own predicted T1, but the real, ground-truth T0 and T1 from the raw training data.**

Take the sequence `T0 T1 T2 T3` (all real tokens in the data):

**Training phase (Teacher Forcing)**

1. Predict T1: input = real T0 → target = real T1;
2. Predict T2: input = **real T0, real T1** → target = real T2;
3. Predict T3: input = **real T0, real T1, real T2** → target = real T3.

👉 Key point: **the context for every prediction is all ground truth (real tokens) from the dataset — the model's own previous outputs are not used!** Even if the model predicts T1 wrong, when computing the next position (predicting T2) it still uses the correct T1 from the source text, not its own wrong prediction.

**Contrast: inference (autoregressive generation, no Teacher Forcing)**:

1. Input T0, the model predicts a T1_hat (its guess);
2. Then **the model's guessed T1_hat** is appended to the context to predict T2;
3. Then the guessed T2_hat continues to predict T3.

**Why can training do this (Teacher Forcing)?**

- Parallel computation: compute all positions' losses at once, no serial generation, vastly faster training;
- Stable gradients: no error accumulation from one wrong step onward.

Downside (a small flaw): during training the model always learns on the "correct context"; at inference the context is the model's own possibly-wrong output. This small distribution gap is called **exposure bias**.

### Question: Why can the losses of all tokens be added together? Is backprop done only once?

> **In one line**: Because the gradient is linear — the total loss's gradient w.r.t. a parameter equals the sum of each position's loss gradient; so they can be added, and training does one forward + one backward, accumulating each position's gradient.

**Total loss = sum (or average) of all positions' losses:**

```
L_total = Σ_i L_i   (cross entropy for predicting the next token at each position)
```

**Why can they be added?** Because the gradient is linear: for any parameter θ:

$$
\frac{\partial L_{total}}{\partial \theta} = \sum_i \frac{\partial L_i}{\partial \theta}
$$

The total-loss gradient w.r.t. a parameter = **sum** of the per-position loss gradients. So adding is perfectly valid.

**Backprop only once?** Yes.

- Not "each position backprops separately", but **one forward computes all positions' losses, one backward accumulates each position's gradient to the same parameter**;
- Each position's gradient is different (each position predicts a different token, different error), but they act on the same set of parameters and are accumulated for one update.

> Your understanding: "compute the losses of T0→T1, T1→T2… and add them" → correct, each position contributes one loss term, and the total is the training target.

**During training, is T1 the real value or the prediction?** Training uses **Teacher Forcing**: T1 is the **real** previous token, not the prediction. This makes all positions' inputs known, enables parallelism, and stabilizes learning.

### Question: When L1 backprops, does the mask come into play? Will it reach T2/T3's QKV?

> **In one line**: No. It is not that backprop has a mask blocking; it is that the forward causal mask severed the information path, so between T0 and T2/T3 there is no computation dependency, and the gradient has no path to travel.

**Core conclusion: the forward causal mask only restricts forward information flow; it does not actively block anything in backprop.** But the effect you get is close to what you guessed: the gradient from L1 (the loss of T0 predicting T1) indeed **does not reach T2/T3's QKV**, but the reason is not "a mask blocks it in backprop", rather **that forward pass never built that computation link**.

1. **Forward, the causal mask acts on attention scores**: when computing T0's attention, the mask blanks out T1, T2, T3. So **T0, when computed, never read T1/T2/T3's embeddings, never used their QKV**. In the computation graph, **T0's output has no edge, no computation dependency with T2/T3**;
2. **Backprop obeys one iron rule**: gradients can only reach what the forward pass actually used; for nodes the forward pass never used, the gradient cannot pass through.

The path of L1: T0 → Transformer → h₀ → lm_head → compute L1. This path never uses any information from T2/T3, so L1's gradient has no route to T2/T3's QKV.

> ⚠️ Key distinction: it is not that backprop has a mask actively intercepting gradients; it is that **the forward causal mask cut the information path at the forward stage, so the computation graph simply has no edge**. With no edge, the gradient has no path.

**But here is a point that is extremely easy to confuse**: the model's QKV weight matrices (parameters Wq, Wk, Wv) are **globally shared**! Although the L1 gradient does not flow into the K/V vectors generated at token T1, the L1 gradient **will definitely act on the globally shared Wq/Wk/Wv matrices**. You must distinguish two layers:

1. **The vectors computed separately per token (q/k/v vectors): intermediate temporary variables** — the L1 gradient can only reach the q/k/v vectors produced at T0, not those at T1/T2/T3;
2. **Wq/Wk/Wv: model parameters, globally shared** — whether T0, T1, or T2 computes q/k/v, they all use this same set of weights. The L1 gradient updates this shared weight, and that modification affects all tokens' subsequent q/k/v computation.

### Question: What are mask, EOS, and padding in training?

> **In one line**: The mask hides the future during training; EOS is the end-of-sequence token — training only predicts up to the real sample's EOS; padding positions do not enter the loss, saving computation.

- **Mask (causal)**: hides future positions during training so each position can only see what precedes it;
- **EOS (end-of-sequence)**: the sequence terminator. **Training only predicts up to the real sample's EOS**; predicting beyond it is meaningless (no ground truth);
- **Padding**: when a sentence's tokens are fewer than the context size, the tail is empty (padding).

**Key understanding (padding/EOS and loss range)**:

- A sentence may have fewer tokens than the context size, the tail is empty (padding);
- **Padding positions do not participate in loss computation** (no meaning — the model should not predict empty slots);
- The model only needs to predict up to **the real training sample's EOS**;
- This **saves a lot of computation** (no need to compute loss and gradients for padding positions).

### Question: How does scaling work, and what are scaling laws?

> **In one line**: Empirically, as compute/data/model size grow, the loss follows a smooth power law; "bigger → lower loss" holds, but only if data grows at the right ratio (Chinchilla) — these are empirical patterns, not mathematical theorems.

Three quantities drive training:

| Quantity | Example | Effect |
|---|---|---|
| Model size N | 7B / 13B / 70B | expressiveness |
| Data size D | 1T / 1.4T tokens | what the model learns |
| Compute C | FLOPs | cost |

Empirical relation: the loss roughly obeys

$$
L \propto C^{-k}
$$

(bigger compute → lower loss, smoothly decreasing power law — the source of the "smooth loss curve" behind emergent ability, see §13).

The classic result **Chinchilla (DeepMind, 2022)**: for compute-optimal scaling, **about 20 training tokens per parameter**:

```
optimal data D ≈ 20 × N  (model size in params)
```

- Chinchilla: 70B params / 1.4T tokens = 20:1
- GPT-3: 175B params / 300B tokens ≈ 1.7:1 (under-trained, far from optimal)

> These are **empirical patterns, not mathematical theorems** — the real loss does not perfectly follow the power law, and emergent ability / saturation are the deviations (see §13).
## 11 Inference: Prefill, Decode, and KV Cache

### Question: What are prefill and decode?

> **In one line**: Generation is split into two phases — prefill computes the prompt in one parallel pass; decode generates tokens one by one; per-token cost is cheap in prefill but expensive in decode.

Given a prompt, generation has two phases:

**Prefill phase (prompt computing)**

- The prompt is given at once: [t₀,t₁,…,tₙ₋₁];
- In **one parallel forward pass**, compute all prompt positions' hidden states and attention;
- Target: produce the hidden state of the last position, from which the first generated token is sampled;
- Complexity: ~O(N²/2) for a length-N prompt (one pass over the whole prompt).

**Decode phase (token-by-token generation)**

- At each step, feed in the newly generated token only, compute its hidden state, sample the next token;
- Repeats until EOS;
- Complexity: O(1) per step, but must be done M times for M generated tokens (O(M) total), serial.

So: prefill is parallel and "cheap per token", decode is serial and "expensive per token".

### Question: What exactly is a hidden state, and where does it live during inference?

> **In one line**: The hidden state is the per-token vector passed between layers — it is a temporary GPU tensor inside each layer's compute, not a cached artifact; only the final layer's hidden state is used to predict the next token.

Think of the hidden state as the model's current "understanding" of each token as it moves through the stack:

```
token embedding  →  h⁽⁰⁾
Layer 1:  LN → QKV → attention → residual → LN → MLP/MoE → residual  →  h⁽¹⁾
Layer 2:  same pipeline on h⁽¹⁾                                       →  h⁽²⁾
...
Layer L:                                                              →  h⁽ᴸ⁾
h⁽ᴸ⁾ of the last token  →  lm_head  →  logits  →  next token
```

Key facts:

- **Lifecycle**: each layer reads the previous layer's hidden state, computes, and produces a new one; the old hidden state is discarded immediately after that layer finishes. It is not stored across layers or across decode steps.
- **What it is used for inside a layer**: after LayerNorm, it is projected into Q, K, V. It is also the base for the residual connection.
- **What is NOT cached**: hidden states are never written to KV Cache. KV Cache stores only the projected K and V tensors.
- **Why earlier prompt tokens must run the full model in prefill**: their K and V (which later tokens attend to) are projected from their hidden states *after* MLP/MoE processing, not from raw embeddings. If earlier tokens skipped MLP, later tokens would attend to unprocessed "raw" vectors.

> **Note**: KV Cache is a byproduct of prefill, not its purpose. Prefill's job is to fully process every prompt token so that the last token's hidden state is rich enough to predict the first generated token; KV Cache just keeps the K/V around for later reuse.

### Question: What exactly is "parallel" in prefill? Is it per-token or per-layer?

> **In one line**: Within one layer, all prompt tokens are processed as a batch (parallel); across layers, execution is strictly serial — layer l must finish all tokens before layer l+1 starts.

The parallelism boundary is easy to get wrong:

```
Layer l:    [h₀, h₁, h₂, ..., hₙ₋₁]   ← whole batch, parallel on GPU
            LN → QKV → attention → MoE/MLP → residual
Layer l out: [h₀', h₁', h₂', ..., hₙ₋₁']   ← must finish ALL tokens first
Layer l+1:  takes [h₀', h₁', h₂', ..., hₙ₋₁'] and repeats
```

- **Layer-internal parallelism**: all tokens in the batch share the same weight matrices; they are packed into big matrices and processed by GPU tensor cores at once. This is why prefill is throughput-friendly.
- **Inter-layer serial dependency**: layer l+1 cannot start until layer l has produced outputs for every token. You cannot finish token 0 through all layers and then start token 1.
- **Causal mask does not break parallelism**: it only restricts which positions can attend to which others; the whole batch still runs as one matrix operation.

In decode, by contrast, each step feeds only one new token — there is no batch dimension to exploit, which is why decode is memory-bandwidth bound rather than compute bound.

### Question: What is KV Cache?

> **In one line**: Caching the K and V computed for already-seen tokens to avoid recomputing them for every new token — the core acceleration technique for decoder inference.

When the decoder generates token by token, each new token's attention needs to attend to all previous tokens. **Their K and V have already been computed in previous steps — so cache them (KV Cache) instead of recomputing.**

- Cache what: only **K and V**, not Q, not QK scores, not embeddings, not attention outputs;
- Where: each layer keeps a growing K_cache, V_cache;
- Benefit: decode cost per step drops from O(T²) to O(T) — a huge speedup for long contexts;
- Memory: KV cache grows linearly with sequence length, at typical ~2×(d_model×heads) bytes per token per layer.

### Question: When QK scoring is done in decode, are the historical scores cached?

> **In one line**: No. QK scores are recomputed on the fly each step; only the raw projected K and V are cached, and the final weighted output is not cached.

Important details:

- The attention score matrix of the past (the QK scores) is **not cached**;
- What is cached is **the raw K and V** (after W_K/W_V projection);
- Each decode step: compute the new token's q, dot it against all cached K to get the current row of scores, softmax, then weight the cached V — only this new row is computed, historical rows are not stored;
- The final weighted output is **not cached** (it is the output, recombined each step from K/V).

### Question: What is the difference between prefill and decode in attention computation?

> **In one line**: Prefill computes the full attention matrix in parallel (all positions attend to all previous positions); decode computes only the current token's row (its q against cached K/V).

| | Prefill | Decode |
|---|---|---|
| Input | whole prompt at once | only the newest token |
| Attention | full matrix, all positions in parallel | only the current token's row |
| K/V | compute then store | reuse cached K/V |
| Complexity | ~O(N²/2) | O(N) per step (with cache) |

### Question: What exactly is stored in the KV cache: raw K/V or after some operation?

> **In one line**: The raw K/V after W_K/W_V projection (the projected values), before any weighted combination; the cached V is the raw projected V, not the attention-weighted output.

Precisely:

- Each layer: W_K/W_V project the current token's X into K and V (the projected values);
- These raw K/V are appended to the cache (before any weighted combination with attention scores);
- At each decode step, the attention weights are computed on the fly and applied to the cached V; the result (the output) is not stored in the cache.

> The cached V is the raw projected V, not the attention-weighted output.

### Question: How does attention attend to itself and avoid the future?

> **In one line**: The causal mask forces position i to attend only to positions 0..i; in decode only past tokens are fed in, so there is no future.

- During training: the **causal mask** forces position i to attend only to positions 0..i (see §10/§12);
- During inference: at decode, only the past tokens are in the context, so there is no future to attend to anyway;
- Prefill uses the causal mask too (only the prompt's past positions).

### Question: How is the KV cache used in prefill and decode respectively?

> **In one line**: Prefill builds the cache once (compute all K/V in one pass); decode reads and appends the cache each step.

- **Prefill**: computes all prompt positions' K/V in one parallel pass and **writes them into the cache** (one fill);
- **Decode**: each step computes only the new token's K/V, **appends** to the cache, and uses the accumulated cache (all previous K/V + the new one) for attention;
- So the cache is built by prefill and extended by decode.

### Question: How does MoE behave under long inputs (prefill)?

> **In one line**: With a very long input, prefill activates almost all experts, losing the MoE's parameter-saving advantage and raising memory cost.

- MoE saves computation by activating only a few experts per token (see §14);
- But in prefill with a very long input, every position must be processed, so almost all experts get activated;
- Result: the "only a few experts are active" advantage is largely lost, and memory/FLOPs grow;
- Industry responds with expert parallelism, expert offloading, better routing, and fine-grained/shared experts (DeepSeek).

## 12 Backpropagation and Loss (Cross-Entropy / softmax+CE Gradient)

### Question: Given a loss, how does backpropagation work? What is the formula?

> **In one line**: Backprop is the most basic multivariate calculus — the chain rule — applied from the loss backward along the computation graph to compute every parameter's partial derivative w.r.t. the loss.

This is the most basic multivariate calculus (the chain rule), applied to neural networks; no additional theory is needed.

- Forward is a chain of composite functions: L = f(…(g(W,x))…);
- Backprop = starting from the loss, going backward along the computation graph, using the chain rule layer by layer to compute each parameter's partial derivative w.r.t. the loss;
- **Addition node: gradient splits directly (each branch the same)**; **multiplication/fork node: gradients add**.

### Question: The most basic numeric example y = wx + b, how does backprop work?

> **In one line**: Forward computes the loss, backward uses the chain rule term by term for the partial derivatives, then gradient descent updates.

```
Let w=3, b=1, x=2, ground truth y_true=8
Forward: y = 3×2 + 1 = 7
L = ½(y−y_true)² = ½(7−8)² = 0.5
Backward:
∂L/∂y = (y−y_true) = 7−8 = −1
∂y/∂w = x = 2
∂L/∂w = ∂L/∂y · ∂y/∂w = (−1)×2 = −2
Gradient descent: w_new = w − η·∂L/∂w = 3 − η·(−2) = 3 + 2η (η=0.1 → 3.2)
```

### Question: Is gradient descent supposed to divide by the slope?

> **In one line**: No. The direction −g is a mathematical constraint; the step size proportional to the gradient (Δw=−ηg) is a chosen strategy, not a mathematical necessity.

**Two different formulas, don't mix them:**

1. **Differential form ΔL ≈ g·Δw**: local analysis, how an infinitesimal change changes the loss;
2. **Update form Δw = −η·g**: descent strategy, walk in the negative-gradient direction.

**"Shouldn't we divide by the slope?"** → No. Clarify:

- **The direction −g** is a mathematical constraint (the negative-gradient direction is where the loss descends fastest — a property of the gradient);
- **The step size proportional to the gradient (Δw = −η·g)** is a **chosen strategy**, not a mathematical necessity. You can choose constant step, AdaGrad, Adam, etc.;
- Keep them apart: **direction is the constraint from the gradient, step size is the strategy**. AdamW adapts the "step size" (momentum + per-parameter scaling).

> In one line: the gradient tells you where to go (direction), η and the optimizer tell you how far (step size). Δw=−ηg uses "bigger gradient, bigger step".

### Question: What if ReLU is non-differentiable?

> **In one line**: ReLU is non-differentiable at x=0, but we take a subgradient and agree on a value (usually 0); since the probability of x being exactly 0 is 0, training practically never hits it.

ReLU(x) = max(0, x), non-differentiable at x=0 (left derivative 0, right derivative 1).

Handling (subgradient):

```
ReLU's "derivative":
x > 0 → 1
x < 0 → 0
x = 0 → take 0 or 1 (just agree on a value)
```

> Your understanding: "an approximate way of approximation" → yes, at x=0 — a measure-zero point — we just pick a value (usually 0) as the derivative. Since the probability of x being exactly 0 is 0 (continuous distribution), training practically never hits it; the effect is negligible. It is not exactly differentiable, but fully workable in engineering.

**Are there functions with slope tending to infinity?** In theory some functions can have a derivative that tends to infinity at isolated points, but **standard activations (ReLU/Sigmoid/GeLU/SwiGLU) have no such "infinite slope"**. They are either piecewise linear (ReLU) or smooth and bounded (Sigmoid/GeLU). So there is no need to worry about "infinite slope".

### Question: At the residual fork/merge points, how do gradients flow? Why "merge points are equal, fork points add"?

> **In one line**: At an additive merge point each input's gradient equals the upstream gradient (addition's derivative is 1); at a multi-way fork point ∂L/∂x = sum of all branches' gradients. All pure partial differentiation/chain rule.

Because y = x + F(x), an addition node. Backprop for x:

$$
\frac{\partial L}{\partial x} = \frac{\partial L}{\partial y} \cdot \frac{\partial y}{\partial x} = \frac{\partial L}{\partial y} \cdot \left(\frac{\partial x}{\partial x} + \frac{\partial F}{\partial x}\right) = \frac{\partial L}{\partial y} \cdot \left(1 + \frac{\partial F}{\partial x}\right)
$$

Split:

- **shortcut branch**: ∂L/∂y × 1 (direct, upstream gradient passed straight back to x);
- **residual-function branch**: ∂L/∂y × ∂F/∂x (into F, continuing to compute attention/MLP gradients).

**The fork (an addition node) "splits flow": the two branches each get the upstream gradient and propagate it on their own.** The "1" of the shortcut is the reason gradient vanishing is prevented.

**Why "merge points are equal, fork points add"?** You are asking about two different nodes:

- **① Fork point (one input flows to multiple downstream)**: e.g., a vector simultaneously participates in attention and goes through the shortcut. In backprop this node receives one gradient from each downstream path and **sums them** as the upward gradient: ∂L/∂x = ∂L/∂y₁ + ∂L/∂y₂;
- **② Merge point (multiple inputs combine into one output via addition)**: e.g., x + attn_out. In backprop, the output's gradient w.r.t. each input = upstream gradient × 1 (addition's derivative is 1), so **each input's gradient equals the upstream gradient (equal)**.

> In one line:
> - Additive merge point (y = a+b): ∂L/∂a = ∂L/∂b = ∂L/∂y (equal, because addition's derivative is 1)
> - Multi-way fork point (x into multiple branches): ∂L/∂x = sum of branch gradients (because x affects all branches)
> This is the truth behind "merge points are equal, fork points add" — pure partial differentiation/chain rule.

### Question: Is this just partial-derivative theory, not unique to neural networks?

> **In one line**: Correct. This is the partial differentiation and chain rule inherent to multivariate calculus, and backprop merely applies this ready-made rule automatically to the computation graph.

This theory is inherently the partial differentiation / chain rule of multivariate calculus — a mathematical conclusion that exists independent of neural networks; it is not a rule invented for backpropagation.

- **The math itself (calculus, unrelated to neural networks)**: as long as a variable x affects the final function L through two different paths, ∂L/∂x = contribution of path 1 + contribution of path 2. As long as y = a+b, ∂L/∂a = ∂L/∂y and ∂L/∂b = ∂L/∂y always hold. Whether it is a physical formula, an economic model, or a neural network, this always holds;
- **Backprop (engineering application)**: a neural network is just a big stack of nested functions (matrix multiplication, softmax, addition, activations, LayerNorm…). Backprop is the automatic traversal of this graph, applying the above calculus rules node by node to compute the partial derivatives of all parameters.

### Question: What is the ground truth (labels) in training?

> **In one line**: Shift the input sequence right by one position, and that is the label; each position's prediction target = the next real token.

Shift the input sequence right by one position, and that is the label. The input at position i is token_i, and the target is token_{i+1}, so the ground truth = the raw sequence shifted right by one:

```
input:  [<s>, I, love, you]
label:  [I, love, you, <eos>]
```

- Each position's prediction target = the next real token;
- This is "autoregressive language modeling": maximize the probability of the real next token.

### Question: What is the full cross-entropy formula?

> **In one line**: Given the ground-truth label y and logits z, softmax gives probability p; cross entropy L = −Σ_k q_k log p_k = −log p_y (one-hot cancels the rest).

Given the ground-truth label y (a token id), the model's output logits z, softmax yields the probability p:

```
softmax:   p_k = e^{z_k} / Σ_j e^{z_j}
one-hot:   q_y = 1 (the true position), rest q_k = 0
cross entropy:    L = −Σ_k q_k · log p_k = −log p_y
```

**It simplifies to −log p_y**: only take the negative log of the probability at the true label (one-hot is 1 only at position y, the other terms multiply by 0 and vanish).

**Properties of the −log p curve**:

- p → 1 (very accurate): −log p → 0, loss small;
- p → 0 (very bad): −log p → ∞, loss large;
- Satisfies "more accurate → smaller loss".

### Question: Why use cross entropy, not MSE or a custom division loss?

> **In one line**: Cross entropy is a "probability-distribution distance", naturally fit for classification; and cross entropy + softmax has an extremely simple gradient (p − one_hot), which is the key reason it is the standard.

- **Cross entropy is a "probability-distribution distance"**: measures the distance between the predicted distribution and the true one-hot distribution, naturally fit for classification (predicting which token is next);
- **MSE**: suited to regression, mismatched for classification's probability target (poor gradient, slow convergence);
- **Custom division loss** (e.g., 1/p_y): numerically unstable, and does not correspond to a probability-based loss.

**Cross entropy + softmax has an extremely simple gradient** (this is the beautiful part), which is why it is the standard.

### Question: What is the merged derivative of softmax + cross entropy?

> **In one line**: ∂L/∂z_k = p_k − 1[k=y], i.e., "predicted probability − true one-hot"; the true position is negative (push up), other positions positive (push down).

Taking the partial derivative w.r.t. logit z_k, the derivatives of softmax and cross entropy cancel into one term:

$$
\frac{\partial L}{\partial z_k} = p_k - 1[k=y] \quad (\text{1[k=y] is an indicator, 1 at the true position})
$$

**Meaning**:

- **True-label position**: p_y − 1 (negative, pushes the logit up so the probability grows);
- **Other positions**: p_k (positive, pushes the logit down so the probability shrinks).

**Numeric example**:

```
z = [2, 1, 0]  →  p = [0.665, 0.245, 0.090]
if y = 2 (the true token is the 3rd)
∂L/∂z = [0.665, 0.245, 0.090 − 1] = [0.665, 0.245, −0.910]
```

(the true position 0.090−1=−0.910 pushes the logit up; other positions are positive, pushing down)

> This is why cross entropy + softmax is so popular: **the gradient is "predicted probability − true one-hot", extremely simple, and tends to 0 as the prediction approaches the true label.**

### Question: How is the lm_head gradient computed?

> **In one line**: logits = h·W_lm_head; ∂L/∂W = g_z·hᵀ (spreads the logit error across every vocabulary dimension), ∂L/∂h = W·g_z (continues to the upper layers).

logits = h · W_lm_head (h is the last hidden vector [d_model], W is [d_model, vocab_size]):

```
g_z = p − one_hot(y)              (the logit gradient computed above, length vocab_size)
gradient of W: ∂L/∂W = g_z · hᵀ
gradient of h: ∂L/∂h = W · g_z
```

- W's gradient spreads the "logit error" across each dimension of the corresponding vocabulary entry;
- h's gradient continues to backprop to the upper layers;
- d_model (e.g., 4096) is usually far smaller than the vocabulary (tens of thousands), so the computation is efficient.

### Question: What are mask, Teacher Forcing, and EOS respectively?

> **In one line**: The mask hides the future during training; Teacher Forcing uses the real previous token during training (not the prediction); EOS is the end-of-sequence token — training only predicts up to the real sample's EOS.

- **Mask**: hides future positions during training so each position can only see what precedes it (see §10);
- **Teacher Forcing**: during training each position directly uses the **real** previous token (ground truth) as input, not the model's own predicted token. This makes training stable and parallel (all positions' inputs are known);
- **EOS (end-of-sequence)**: the sequence terminator. **Training only predicts up to the real sample's EOS**; predicting beyond it is meaningless (no ground truth).

**Your key understanding (padding/EOS and loss range)**:

- A sentence's tokens may be fewer than the context size, the tail empty (padding);
- **Padding positions do not participate in loss computation** (no meaning — the model need not predict empty slots);
- The model only needs to predict up to **the real training sample's EOS**;
- This **saves a lot of computation** (no loss/gradient at padding positions).

### Question: When predicting T2 during training, is the T1 the model's prediction or the real T1?

> **In one line**: The real T1 (Teacher Forcing). Training uses the real ground-truth context, not the model's previous prediction; inference uses the model's own generated tokens.

This is the classic confusion point. **During training, the input used to predict T2 is not the model's own predicted T1, but the real, ground-truth T0 and T1 from the raw training data.**

Take the sequence `T0 T1 T2 T3` (all real tokens in the data):

**Training phase (Teacher Forcing)**

1. Predict T1: input = real T0 → target = real T1;
2. Predict T2: input = **real T0, real T1** → target = real T2;
3. Predict T3: input = **real T0, real T1, real T2** → target = real T3.

👉 Key point: **the context for every prediction is all ground truth (real tokens) from the dataset — the model's own previous outputs are not used!** Even if the model predicts T1 wrong, when computing the next position (predicting T2) it still uses the correct T1 from the source text, not its own wrong prediction.

**Contrast: inference (autoregressive generation, no Teacher Forcing)**:

1. Input T0, the model predicts a T1_hat (its guess);
2. Then **the model's guessed T1_hat** is appended to the context to predict T2;
3. Then the guessed T2_hat continues to predict T3.

**Why can training do this (Teacher Forcing)?**

- Parallel computation: compute all positions' losses at once, no serial generation, vastly faster training;
- Stable gradients: no error accumulation from one wrong step onward.

Downside (a small flaw): during training the model always learns on the "correct context"; at inference the context is the model's own possibly-wrong output. This small distribution gap is called **exposure bias**.

### Question: Why can all tokens' losses be added? Is backprop done once?

> **In one line**: Because the gradient is linear — the total loss's gradient w.r.t. a parameter equals the sum of each position's loss gradient; so they can be added, and training does one forward + one backward, accumulating each position's gradient.

**Total loss = sum (or average) of all positions' losses:**

```
L_total = Σ_i L_i   (cross entropy for predicting the next token at each position)
```

**Why can they be added?** Because the gradient is linear: for any parameter θ:

$$
\frac{\partial L_{total}}{\partial \theta} = \sum_i \frac{\partial L_i}{\partial \theta}
$$

The total-loss gradient w.r.t. a parameter = **sum** of the per-position loss gradients. So adding is perfectly valid.

**Backprop only once?** Yes.

- Not "each position backprops separately", but **one forward computes all positions' losses, one backward accumulates each position's gradient to the same parameter**;
- Each position's gradient is different (each position predicts a different token, different error), but they act on the same set of parameters and are accumulated for one update.

> Your understanding: "compute the losses of T0→T1, T1→T2… and add them" → correct, each position contributes one loss term, and the total is the training target.

**During training, is T1 the real value or the prediction?** Training uses **Teacher Forcing**: T1 is the **real** previous token, not the prediction. This makes all positions' inputs known, enables parallelism, and stabilizes learning.

### Question: When L1 backprops, does the mask come into play? Will it reach T2/T3's QKV?

> **In one line**: No. It is not that backprop has a mask blocking; it is that the forward causal mask severed the information path, so between T0 and T2/T3 there is no computation dependency, and the gradient has no path to travel.

**Core conclusion: the forward causal mask only restricts forward information flow; it does not actively block anything in backprop.** But the effect you get is close to what you guessed: the gradient from L1 (the loss of T0 predicting T1) indeed **does not reach T2/T3's QKV**, but the reason is not "a mask blocks it in backprop", rather **that forward pass never built that computation link**.

1. **Forward, the causal mask acts on attention scores**: when computing T0's attention, the mask blanks out T1, T2, T3. So **T0, when computed, never read T1/T2/T3's embeddings, never used their QKV**. In the computation graph, **T0's output has no edge, no computation dependency with T2/T3**;
2. **Backprop obeys one iron rule**: gradients can only reach what the forward pass actually used; for nodes the forward pass never used, the gradient cannot pass through.

The path of L1: T0 → Transformer → h₀ → lm_head → compute L1. This path never uses any information from T2/T3, so L1's gradient has no route to T2/T3's QKV.

> ⚠️ Key distinction: it is not that backprop has a mask actively intercepting gradients; it is that **the forward causal mask cut the information path at the forward stage, so the computation graph simply has no edge**. With no edge, the gradient has no path.

**But here is a point that is extremely easy to confuse**: the model's QKV weight matrices (parameters Wq, Wk, Wv) are **globally shared**! Although the L1 gradient does not flow into the K/V vectors generated at token T1, the L1 gradient **will definitely act on the globally shared Wq/Wk/Wv matrices**. You must distinguish two layers:

1. **The vectors computed separately per token (q/k/v vectors): intermediate temporary variables** — the L1 gradient can only reach the q/k/v vectors produced at T0, not those at T1/T2/T3;
2. **Wq/Wk/Wv: model parameters, globally shared** — whether T0, T1, or T2 computes q/k/v, they all use this same set of weights. The L1 gradient updates this shared weight, and that modification affects all tokens' subsequent q/k/v computation.
## 13 Overall Reflections on the Transformer

> This section collects the broader discussions: emergent ability, distillation, and the "point cloud" view. 精简回答见引用块；详细回答保留公式、数字例与对照。

### Question: What is emergent ability? Is it a "wake-up"?

> **In one line**: On some tasks, small models score near random; once scale crosses a critical point, accuracy suddenly jumps; but the underlying predictive loss decreases smoothly and continuously without a jump — continuous progress surfacing as "suddenly can do it" on discrete tests.

**In one line: on some tasks, small models score near random; when scale crosses a critical point, accuracy suddenly rises sharply. This "from not-can to can" jump is emergence.**

**Key point: the underlying predictive loss decreases smoothly and continuously, with no jump!** But the discrete tests we design (right/wrong) make the continuous tiny progress accumulate to a threshold and "suddenly can do it".

**Why emergence? Two mainstream explanations**:

1. **The building-block theory**: reasoning tasks decompose into many small sub-skills (recognize digits, align, add per-digit, carry, assemble result). Small models learn only a few, never enough to do it right; the moment all are assembled, the whole thing jumps;
2. **Fitting an implicit algorithm in high-dimensional vector space**: with enough parameters, the model can store/call a vector-operation-like algorithm.

**Misconceptions cleared**:

- Emergence ≠ the model produces understanding/consciousness; it is still statistics + high-dimensional vector operations;
- Not all abilities emerge: basic abilities improve smoothly; multi-step reasoning/arithmetic/complex instructions are the easiest to emerge;
- Emergence is not from nowhere; it still follows scaling laws.

**Inverse Scaling**: on some simple tasks, larger models can get worse (small models can't see the misleading trap and answer directly; large models learn the misleading pattern and fall into it).

### Question: Is distillation stealing weights?

> **In one line**: No. Model distillation has a teacher (large model) teach a student (small model); the student learns the teacher's output probability distribution rather than raw hard labels — it is not copying weights or stealing parameters.

**Model distillation is not copying weights, not stealing parameters.** A large model (teacher) teaches a small model (student); the student learns **the teacher's output probability distribution** instead of raw hard labels.

> Example: the teacher outputs "cat 0.7, dog 0.2, bird 0.05", the small model fits this probability distribution, which is more informative than fitting only the single correct answer.

Clarify:

1. Distillation ≠ weight stealing — just call the teacher API for output probabilities;
2. Distillation ≠ the only route; LLaMA/DeepSeek's main versions are **natively pretrained** (trained from scratch on massive text), not distilled;
3. Distillation is mostly for large→small compression; pure distillation rarely surpasses the teacher.

### Question: Does each token become a "point cloud"? Are different models' point clouds related?

> **In one line**: Yes. The model turns each token into a high-dimensional vector; all vectors distribute in high-dimensional space as a point cloud; different models' point clouds are "the same semantic structure projected in different coordinate systems" — relative structure similar, absolute coordinates different.

**What is a token point cloud**:

- The model turns each token into a high-dimensional vector (embedding);
- All tokens' vectors distribute in high-dimensional space, forming a "point cloud";
- Semantically similar tokens sit close together (cat/dog/pet close; "run/walk/dash" close).

**Several key properties of the point cloud**:

1. **Not randomly distributed**: after learning semantic structure, the point cloud shows meaningful clusters and directions;
2. **Directions carry semantics**: the classic "king − queen ≈ man − woman" vector arithmetic is the point cloud's consistent directions;
3. **Different layers, different point clouds**: shallow layers reflect lexical/surface structure, deep layers reflect semantics/abstraction — a "layer-by-layer reshaping";
4. **Input X also contains position**: the same word at different positions has a different vector (position encoding perturbs it), so the point cloud is more complex.

**Are different models' point clouds related?**

- **Not identical, but highly similar**: as long as data is sufficient and training thorough, semantically similar tokens tend to sit close in every model;
- Because the semantic structure of language itself is objective (cat and dog are closer than cat and car), different models "discover" the same structure from different angles;
- But the **coordinate spaces differ**: model A and model B have different vector dimensions and coordinate systems; you cannot compare the numbers directly, only the "relative structure" (distance, direction, clustering);
- Some research uses "representation alignment" to find mappings between different models' point clouds and finds the underlying semantic structure is shared.

> In one line: different models' point clouds are "the same semantic structure projected in different coordinate systems"; relative structure similar, absolute coordinates different.

**How the point cloud connects to the architecture above**:

- Attention weighted sum = "mixing by similarity" within the point cloud;
- Residual addition = vector translation/stacking in the point cloud;
- MLP nonlinearity = nonlinear deformation of the point cloud;
- LayerNorm = per-token standardization: each point is independently shifted to its own center and rescaled (the point cloud's shape does change; the purpose is numeric stability, not geometry preservation).

### Question: Is everything operations in a high-dimensional vector space?

> **In one line**: Yes. From embedding to attention to residual, the whole Transformer is linear combinations, similarity comparisons, and nonlinear deformations in a high-dimensional vector space.

- **"Everything is operations in a high-dimensional vector space"**: from embedding to attention to residual, the whole Transformer is linear combinations, similarity comparisons, and nonlinear deformations in high-dimensional vector space;
- **Why weighted summation is the most basic math**: not a special theory, just linear algebra's linear combination, used in attention;
- **The Transformer's "inductive bias"**: self-attention learns associations, MLP provides nonlinearity, LayerNorm stabilizes values — these designs together determine "what the model tends to learn".

## 14 MoE (Supplement · non-classical Transformer)

> MoE does not belong to the classic Transformer; it is placed after the whole study as a supplement, and is also a direction for future expansion of this document.

### Question: Why put MoE as a supplement?

> **In one line**: The classic Transformer is dense (one set of QKV, one MLP per layer; every token runs all parameters); MoE is a later architectural improvement that replaces the MLP with multiple experts and activates only a few — an "advanced" topic.

- The classic Transformer is **dense**: one set of QKV, one MLP per layer; every token runs all parameters;
- **MoE (Mixture of Experts)** is a later architectural improvement: the MLP is replaced by multiple experts and only a few are activated;
- So learn the classic Transformer first, then MoE as an "advanced" supplement.

### Question: How are activated parameters computed in MoE? Does it correspond to QKV internal activation or the whole activation?

> **In one line**: The Chinchilla 20:1 rule applies to MoE by the **per-token activated parameters** (active params), not total params; Attention (QKV) is dense and fully activated, only MLP does sparse MoE.

**MoE's 20:1 rule (Chinchilla) applies to the per-token activated parameters (active params), not total params.**

- **N_total total params**: all experts added together (all stored in memory);
- **N_active activated params**: the params actually involved in each token's computation.

Example Mixtral-8x7B: total 46.7B, 2 experts per token, Active≈13B. Optimal ratio: 20 tokens : 1 activated param → 13B×20 = 260B tokens.

**Key boundary**:

- ✅ **Attention (QKV) is dense**: only one set of WQ/WK/WV per layer, used by every token, always activated;
- ✅ **MLP is MoE**: one layer has N experts, the router selects K per token, only those K expert MLPs are activated.

> **QKV is fully activated; there is no partial activation.** No portion of the matrix elements is sliced off. Only a few very special architectures put QKV into experts (mainstream Mixtral does not).

### Question: Are "active parameters" and "activation" the same thing?

> **In one line**: No. Active parameters are the activated weight parameters (model weights); activation (激活值) is the intermediate vector output by a matrix operation (data, not parameters).

- active **parameters**: the activated weight parameters (model weights involved in computation);
- activation (激活值): the intermediate vector output by a matrix operation (data, not parameters).

### Question: What is the difference between dense and MoE models?

> **In one line**: Dense models have total params = activated params (100% activated, no need to distinguish); MoE has N_active < N_total, and activated params are the key to compute cost and the Chinchilla ratio.

- **Dense model**: total = activated params, 100% activated, no need to distinguish;
- **MoE**: N_active < N_total, and the activated params are the key to compute cost and the Chinchilla ratio.

**Does MoE's total params mean more tokens can be matched?** No. Compute cost is determined by activated params; large total params do not mean large compute. MoE's advantage: under the same compute, the total knowledge capacity is larger (experts store different types of knowledge in partitions).

**MoE's pitfalls**: total params too large → easy to overfit (look at total params, not activated); expert load-balancing problems (routing skews, wasting params).

### Question: Why does the industry insist on MoE?

> **In one line**: Larger total knowledge capacity + lower decode cost; expert parallelism, expert offloading, better routing, fine-grained/shared experts (DeepSeek) address the prefill problem.

- Larger total knowledge capacity + lower decode cost;
- Expert parallelism, expert offloading, better routing, fine-grained/shared experts (DeepSeek) address the prefill problem;
- See §11 (MoE with very long inputs activates almost all experts).

### Question: What is the exact data flow inside one MoE layer?

> **In one line**: LN → x_norm branches in parallel to the Gate (router) and to all experts; the Gate only outputs indices + weights, it does not forward the tensor to selected experts — unselected experts simply skip computation.

In a Pre-LN Transformer block, after attention:

```
x_in (residual input)
├─ residual branch: x_in passes through untouched
└─ main branch:
    1. LayerNorm → x_norm
    2. x_norm fans out in parallel:
       ├─ to Gate:   scores = softmax(W_g · x_norm) → pick top-K expert indices + weights
       └─ to all N experts: each expert receives x_norm
    3. Only the K selected experts actually run their MLP; the rest skip computation
    4. y_moe = Σ (weight_i · Expert_i(x_norm))  over the K selected experts
    5. x_out = x_in + y_moe
```

**Key point**: the Gate is not a switch that routes the tensor. It only outputs routing metadata (which experts, with what weights). The tensor is broadcast to all experts; unselected ones just do not execute their matrix multiplication.

> **Note**: In GPU implementations this is done via token permutation — tokens routed to the same expert are batched together, and other tokens simply never enter that expert's compute.

### Question: How does the Gate decide which experts to activate?

> **In one line**: a small linear layer W_g scores x_norm → softmax → pick the top-K scores; those indices schedule which experts run, the weights weight their outputs.

1. **Score**: `s = W_g · x_norm` → one score per expert, W_g shape is (hidden_dim → num_experts), very small.
2. **Softmax**: `w_i = softmax(s)_i`, weights sum to 1.
3. **Top-K selection**: keep the K largest weights and their expert indices; K is usually 2.
4. **Output**: `y_moe = Σ_{i in topK} w_i · Expert_i(x_norm)`.

The Gate's job is purely this routing decision; it does not modify or forward the input tensor.

### Question: What are the main benefits of MoE?

> **In one line**: decouple total parameter count from per-token compute; let different experts specialize on different token types; scale training across many GPUs.

1. **Decouple capacity from compute**: a dense model that doubles params roughly doubles per-token FLOPs; MoE can have 8× experts but each token only runs 2× experts — much larger knowledge capacity at modest compute cost.
2. **Expert specialization**: the Gate learns to route different token types to different experts (math, code, narrative, translation), so each expert only needs to learn a narrow pattern.
3. **Training parallelism**: experts can be sharded across GPUs, scaling training beyond what a single dense model fits.

**Costs**: all expert weights still live in memory (large VRAM); routing imbalance can overload one expert; the compute graph becomes data-dependent (different tokens take different paths).

### Question: How are parameters distributed between experts and attention?

> **In one line**: experts dominate — roughly 90%+ of total weights are in the MoE experts; attention (QKV + W_O) is only ~8% because it stays dense and does not grow with expert count.

| Model type | Experts / MLP share | Attention (QKV+W_O) share |
|---|---|---|
| Dense LLaMA 7B | ~65% | ~30% |
| Mixtral 8×7B MoE | ~92% | ~8% |
| OLMoE | ~93% | ~3.9% |
| DeepSeek V4-Pro | ~97% | ~12% |

**Why**: QKV has a fixed size regardless of how many experts you add. All new parameters introduced by MoE go into the expert MLPs.

> **Note**: this is model *weight* memory. At inference, long-context decode is often limited by **KV Cache** (which scales with sequence length, not total params) — a separate memory budget from the expert weights.

### Question: How does backpropagation work in MoE?

> **In one line**: only the K experts activated in the forward pass receive gradient and update; unselected experts get zero gradient from this token; the Gate always receives gradient.

Forward: `y_moe = Σ_{i in active} w_i · Expert_i(x_norm)`.

Backward from the loss:

1. Gradient reaches `y_moe`, then splits: `∂L/∂y_i = w_i · ∂L/∂y_moe` for the K active experts.
2. Each active expert runs normal MLP backprop and updates its own weights.
3. Unselected experts: `∂L/∂y_j = 0` → no gradient, no update this step.
4. The Gate (W_g) always receives gradient, because its weights `w_i` participated in the weighted sum — this includes both the main task loss and the auxiliary load-balancing loss.

> **Note on the discrete top-K**: the top-K index selection itself is non-differentiable. In practice, gradients flow through the continuous softmax weights (Straight-Through Estimator) — the forward pass uses a hard pick, but backward uses the soft weights to train the Gate.

### Question: What does "experts are independent" actually mean?

> **In one line**: experts have separate weights (no sharing), but they are trained jointly in one global training run — the same loss, the same optimizer, just sparse per-token gradient updates.

- **Independent = weights are separate**: Expert_1's W_1/W_2 are different tensors from Expert_2's; updating one does not touch the other.
- **Not independently trained**: there is one global cross-entropy loss; all experts + Gate + Attention are in one model, optimized together.
- Each token only updates the K experts it was routed to; different batches route different tokens to different experts, so over time every expert gets trained on the samples it specializes in.
- The only coupling between experts is the shared Gate, which learns how to divide the work — plus the load-balancing loss that prevents all tokens from piling onto a few experts.

## 15 Quick Reference: Core Formulas and Common Pitfalls

> This is the quick-reference appendix (not Q&A): formula tables, comparison tables, common-pitfall checklist, and core numeric examples. Section numbers correspond to the 15-chapter structure.

### Core formula quick reference

| Concept | Formula | Note | Section |
|---|---|---|---|
| Input vector | X = TokenEmb(token) + PosEmb(pos) | addition, GPT-2 learnable positions | 01/03 |
| QKV projection | Q=XW_Q, K=XW_K, V=XW_V | three independent trainable matrices | 04 |
| Attention | softmax(QKᵀ/√d_k)V | QK scores, softmax weights, weighted V | 04 |
| QK score | score_ij = Q_i·K_jᵀ | dot product = direction similarity | 04 |
| Multi-head split | d_k = d_model / num_heads | QKV same length within a head | 05 |
| Attention scaling | divide by √d_k | prevents high-dim dot-product variance inflation | 03 |
| Residual | x_out = x + F(x) | shortcut, prevents gradient vanishing | 08 |
| Residual gradient | ∂L/∂x = ∂L/∂y·(1+∂F/∂x) | fork; shortcut contributes "1" | 08/12 |
| softmax+CE gradient | ∂L/∂z_k = p_k − 1[k=y] | extremely simple, prediction minus true one-hot | 12 |
| Cross entropy | L = −log p_y | simplified to the true position only | 12 |
| lm_head gradient | ∂L/∂W = g_z·hᵀ; ∂L/∂h = W·g_z | g_z = p − one_hot(y) | 12 |
| Gradient descent | Δw = −η·g | direction negative gradient (constraint), step size strategy | 10/12 |
| Chinchilla | D ≈ 20N | ~20 training tokens per param (dense) | 10/14 |
| Scaling law | L ∝ C^(−k) | power law, smooth decrease | 10 |

### Key comparison tables

**QKV / matrix multiplication vs dot product**

| | Matrix multiplication X·W | Dot product Q·K |
|---|---|---|
| Output | new vector | scalar |
| Role | linear mapping, generates QKV | similarity scoring |
| Negative | — | can be ±; softmax always positive |

**Position encoding**

| | GPT-2 | LLaMA/DeepSeek |
|---|---|---|
| Method | learnable table, added to X | RoPE rotating Q/K |
| Position enters | X (before attention) | Q, K (not V) |

**Float precision**

| | Training | Inference |
|---|---|---|
| Mainstream | BF16 | FP8 / INT8 |
| Note | BF16 exponent matches FP32 | quantization saves memory |

**Dense vs MoE**

| | Dense | MoE |
|---|---|---|
| Activated params | = total (100%) | < total |
| What changes | all | only MLP (QKV dense) |

**Training vs inference**

| | Training | Inference |
|---|---|---|
| Mode | parallel batch | token-by-token serial |
| View | mask hides future | only past |
| Complexity | O(N²/2) | O(N) (KV cache) |
| T2 embedding | all in place | looked up after generation |

**Merge point vs fork point**

| Node | Gradient behavior |
|---|---|
| Additive merge (y=a+b) | each input gradient = upstream gradient (equal) |
| Multi-way fork (x into branches) | ∂L/∂x = sum of branches |

### Common-pitfall checklist

1. V is not one extracted entry — it is the weighted mixture of all V (§04)
2. The model does not pre-store QKV — inference computes them on the fly (§04)
3. Dot product can be negative; softmax weights always positive (§03)
4. X=embedding+position is addition; RoPE is not added to X (§03)
5. Attention's QK scoring and weighted V are linear, but softmax is nonlinear; the MLP adds activation nonlinearity (§07)
6. KV Cache stores only K/V, not Q/QK/embedding (§11)
7. The cached V is the raw projected V, not the weighted output (§11)
8. The residual adds the pre-LN raw x; two residuals per layer (§08)
9. ReLU takes an agreed value at 0 (subgradient); standard activations have no infinite slope (§07/12)
10. Additive merge-point gradients are equal; multi-way fork-point gradients add (§08/12)
11. Gradient-descent direction is a constraint, step size is a strategy (Δw=−ηg is a strategy) (§12)
12. Ground truth = input shifted right by one (§12)
13. softmax+CE gradient = p − one_hot(y) (§12)
14. Training uses Teacher Forcing (real previous token), not the prediction (§12)
15. Padding positions do not enter the loss; only up to EOS, saving computation (§10/12)
16. Total-loss gradient = sum of per-position gradients; one backprop accumulates (§12)
17. Chinchilla 20:1 applies to MoE by activated params (§10/14)
18. MoE only changes the MLP; Attention QKV is dense and always activated (§14)
19. Scaling law is an empirical pattern, not a mathematical theorem (§10)
20. Emergence is a continuous loss jumping on a discrete task, not a wake-up (§13)
21. GPT is decoder-only, no encoder/cross-attention (§11)
22. Different models' point clouds: relative structure similar, absolute coordinates different (§13)

### Core numeric examples to remember

- QKV 3-dim example: Q=K=I, V=diag(10,20,30) → Out_i = V_i (§04)
- Mixture example: Q₀ scores K=[1,1,0] → scores [1,1,0] → softmax [0.422,0.422,0.155] → Out₀=[4.22,8.44,4.65] (§04)
- y=wx+b: w=3,b=1,x=2,y_true=8 → L=0.5 → ∂L/∂w=−2 → w_new=3.2 (η=0.1) (§12)
- softmax+CE: z=[2,1,0]→p=[0.665,0.245,0.090], y=2→g_z=[0.665,0.245,−0.910] (§12)
- GPT-2: d_model=768, 12 heads, d_k=64; LLaMA-7B: 4096, 32 heads, d_k=128 (§05)
- Chinchilla: 70B/1.4T=20:1; GPT-3=175B/300B≈1.7 (under-trained) (§10)
- Mixtral-8x7B: total 46.7B, activated≈13B, optimal tokens≈260B (§14)

## Related knowledge

- [[topics/daily-contribution-tawg|Daily Contribution and Settlement TAWG]]

The relationships above are navigation aids; factual claims are limited to the cited evidence.
