# MuseFactory 仕様・実装指示書

> この文書は Claude Code(VSCode)向けの実装指示書です。**フェーズ順に実装し、各フェーズ完了時に「確認手順」を実行して報告してから次へ進むこと。** 不明点は推測で進めず質問すること。

---

## 0. 概要

LM Studio(OpenAI互換API)で動くローカルLLMを使い、画像・動画生成AI向けのプロンプトテキストファイルを作成・加工するデスクトップGUIアプリ。

### 主な機能
1. **ざっくり指示から生成**:曖昧な日本語の指示から、指定したモデル向けのプロンプトファイルを指定フォルダに作る
2. **抽出**:既存の複数プロンプトファイルから特定要素(ポーズ・表情など)を抜き出して新ファイルを作る
3. **加工**:盛る(詳細化)、英訳、別モデル向けへの書き換え、画像用→動画用への変換
4. **ストーリーモード**:「ファンタジーで勇者が魔王と戦ってる一連のやつ」のような指示から、話の流れのある一連のプロンプトを連番で作る
5. **パーツライブラリ**:抽出した要素をカテゴリ別に蓄積し、組み合わせて再利用する

### 設計の柱
- **中間表現(JSON)と出力整形を分離する。** 新しい画像・動画モデルへの対応は「モデルプロファイル」ファイルの追加だけで済むようにする
- **LLM側も「LLMプロファイル」で設定化する。** 接続先・モデル・特性をファイルで管理し、タスクごとに割り当てる
- 8GB VRAMのLLM専用機が主力であることを前提に、**小さいモデルでも安定する作り**にする

---

## 1. 動作環境・前提

| 項目 | 内容 |
|---|---|
| OS | Windows 11 |
| 言語 | Python 3.11以上 |
| GUI | **既存アプリ PromptWordCrafter と同じGUIフレームワーク・画面構成・見た目を踏襲する**(実装前に PromptWordCrafter のソースを読み、使用フレームワーク・レイアウト・配色・ウィジェット構成を把握してから着手すること) |
| LLM | LM Studio(OpenAI互換API `/v1/chat/completions`, `/v1/models`) |
| 使用LLM | Qwen 9B、Qwen 14B、Gemma(ビジョン対応版を含む) |
| 対象の画像生成モデル | Krea2、Qwen2.1、Qwen2511、Z-Image Turbo など(いずれも自然文プロンプト中心) |
| 対象の動画生成モデル | Minimax H3、Wan2.2 など |

### LAN構成

| 名前 | IP | GPU | 役割 |
|---|---|---|---|
| LLM機 | 192.168.55.3(`http://192.168.55.3:1234/v1`) | RTX 5060 8GB | LM Studio常駐(主力) |
| PC1 | 192.168.11.31 | RTX 4060 Ti 16GB | ComfyUI。空いていればLLMにも使える |
| PC2 | 192.168.11.30 | RTX 4060 Ti 16GB | ComfyUI。空いていればLLMにも使える |
| PC3 | 192.168.11.14 | RTX 4060 8GB | ComfyUI |

※IPはコードに直書きせず、すべて設定ファイル(LLMプロファイル)で管理する。

---

## 2. ディレクトリ構成(案)

```
MuseFactory/
├─ main.py
├─ app/
│  ├─ ui/                 # GUI(PromptWordCrafter踏襲)
│  ├─ core/
│  │  ├─ llm_client.py    # LM Studio通信・再試行・<think>除去・JSON修復
│  │  ├─ router.py        # タスク→LLM割り当て、接続先フォールバック
│  │  ├─ job_queue.py     # 直列キュー、モデル別まとめ実行
│  │  ├─ intermediate.py  # 中間表現の定義・検証
│  │  ├─ renderer.py      # 中間表現→モデル別プロンプト文
│  │  ├─ tasks/           # generate / extract / enhance / translate / convert / story
│  │  └─ library.py       # パーツライブラリ
│  └─ util/               # ファイル名生成、トークン/文字数計測、ログ
├─ profiles/
│  ├─ targets/            # 画像・動画モデルのプロファイル(*.json)
│  └─ llms/               # LLMプロファイル(*.json)
├─ library/               # パーツライブラリ保存先
├─ config/settings.json   # タスク割り当て・既定フォルダ等
├─ logs/
└─ tests/
```

---

## 3. データ仕様

### 3.1 中間表現(Intermediate)

すべての機能はこの形式を経由する。**小さいモデルでも崩れにくいよう、入れ子は浅く、値は短い文字列で。**

```json
{
  "id": "uuid",
  "kind": "image",
  "subject": "silver-haired young hero in a blue cape and scarred armor",
  "pose": "",
  "expression": "",
  "outfit": "",
  "background": "",
  "lighting": "",
  "camera": "",
  "style": "",
  "motion": "",
  "camera_motion": "",
  "timeline": [],
  "extra": "",
  "negative": "",
  "source": {"type": "generate", "files": [], "instruction": ""},
  "lang": "en"
}
```

- `kind`: `"image"` または `"video"`
- `motion` / `camera_motion` / `timeline` は動画用。`timeline` は `[{"start": 0, "end": 2, "action": "..."}]`
- 空の項目は空文字。未知の項目は `extra` に入れる
- 出力プロンプトファイルと同名の `.json` として**中間表現も保存する**(別モデル向けに出し直せるようにするため。設定でON/OFF可)

### 3.2 モデルプロファイル(`profiles/targets/*.json`)

```json
{
  "name": "Wan2.2 I2V",
  "type": "video",
  "mode": "i2v",
  "output_language": "en",
  "style": "natural_sentence",
  "max_length": 120,
  "negative_supported": true,
  "element_order": ["subject", "motion", "camera_motion", "lighting", "style"],
  "skip_elements": ["outfit", "background"],
  "prefix": "",
  "suffix": "",
  "system_prompt": "You write video prompts for Wan2.2 image-to-video. Describe only motion and camera movement...",
  "example_prompts": ["...", "..."],
  "temperature": 0.7,
  "reference_image_mode": false
}
```

| 項目 | 説明 |
|---|---|
| `type` / `mode` | `image`/`video`、`t2i`/`t2v`/`i2v`/`edit` など |
| `style` | `natural_sentence`(自然文)/ `tags`(タグ列)。将来のタグ系モデル用 |
| `max_length` | 単語数の目安。超過時はUIで警告 |
| `negative_supported` | falseならネガティブを出力しない(Turbo・蒸留系でCFG1運用のモデル向け) |
| `skip_elements` | このモデルでは書かない要素(I2Vでは見た目は画像で決まるため等) |
| `example_prompts` | お手本。**few-shotとしてLLMに渡す。最重要** |
| `reference_image_mode` | 参照画像・LoRA併用を前提にした書き方にするか |

- 起動時に `profiles/targets/` を読み込み、UIのモデル選択肢に並べる
- 初期プロファイルとして、対象モデルぶんの雛形を同梱する(`system_prompt` と `example_prompts` は仮でよい。後でユーザーが調整する)

### 3.3 LLMプロファイル(`profiles/llms/*.json`)

```json
{
  "name": "Qwen 9B @LLM機",
  "endpoint": "http://<LLM機IP>:1234/v1",
  "model_id": "qwen-9b-instruct",
  "supports_json_schema": true,
  "supports_vision": false,
  "thinking": "strip",
  "system_prompt_mode": "system",
  "default_temperature": 0.7,
  "context_length": 8192,
  "timeout_sec": 180
}
```

| 項目 | 説明 |
|---|---|
| `thinking` | `"strip"`:`<think>…</think>` を出力から除去 / `"off"`:思考を無効化する指示を付ける / `"keep"` |
| `system_prompt_mode` | `"system"`:system roleで送る / `"merge_user"`:重要な指示をuserメッセージにも重ねる(Gemma等、system roleが効きにくいモデル用) |
| `supports_json_schema` | trueなら `response_format` にJSON Schemaを指定。falseなら指示文で依頼して後でパースする |

### 3.4 タスク割り当て(`config/settings.json`)

```json
{
  "task_routing": {
    "extract":        ["Qwen 9B @LLM機"],
    "translate":      ["Qwen 9B @LLM機"],
    "render":         ["Qwen 9B @LLM機"],
    "enhance":        ["Qwen 9B @LLM機"],
    "story_plan":     ["Qwen 14B @PC1", "Qwen 14B @PC2", "Qwen 14B @LLM機", "Qwen 9B @LLM機"],
    "image_to_prompt":["Gemma Vision @LLM機"]
  },
  "comfyui_hosts": {
    "Qwen 14B @PC1": "http://192.168.11.31:8188",
    "Qwen 14B @PC2": "http://192.168.11.30:8188"
  },
  "save_intermediate_json": true,
  "default_output_dir": ""
}
```

- 各タスクの値は**優先順位つきの候補リスト**(フォールバック用、4.2参照)
- `comfyui_hosts`:そのLLM接続先と同じPCのComfyUI。生成中かどうかの判定に使う

---

## 4. コア処理の仕様

### 4.1 LLMクライアント(`llm_client.py`)
- OpenAI互換APIで `chat/completions` を呼ぶ
- `thinking: strip` のとき、応答から `<think>…</think>` を除去
- JSONを要求するタスクは:
  1. `supports_json_schema` ならスキーマ指定
  2. パース失敗 → コードブロック除去、末尾カンマ除去など簡易修復 → 再パース
  3. それでも失敗 → 同じリクエストを1回だけ再試行
  4. それでも失敗 → エラーとしてログ・UIに表示(処理全体は止めない)
- タイムアウトと接続エラーは例外クラスを分けて上位に返す

### 4.2 ルーター(`router.py`)
タスク実行時、候補リストを先頭から順に試す:
1. `GET {endpoint}/models` で応答があるか確認(短いタイムアウト)
2. `comfyui_hosts` に対応があれば `GET /queue` を確認し、`queue_running` が空でなければ**生成中なのでスキップ**
3. 使える最初の接続先を採用。全滅ならエラー
- 結果はキャッシュ(30秒程度)して、毎回問い合わせない
- **UIのステータス欄に「実行中:タスク名/LLMプロファイル名」を常に表示する**

### 4.3 ジョブキュー(`job_queue.py`)
- LLMへのリクエストは**1件ずつ直列**で送る(LLM機は他PCからも使われるため)
- 一括処理はジョブを**採用されたLLMプロファイルごとにまとめて並べ替えて**実行(8GB VRAMではモデルの入れ替えに時間がかかるため)
- 進捗(n/N)、キャンセル、失敗ジョブのスキップと一覧表示
- UIスレッドを止めないこと(ワーカースレッド+シグナル等、使用GUIフレームワークの作法に従う)

### 4.4 レンダラー(`renderer.py`)
中間表現+モデルプロファイル → プロンプト文:
- LLMに渡すもの:プロファイルの `system_prompt`、`example_prompts`(few-shot)、`element_order` 順に並べた要素(`skip_elements` は除外)、`max_length`、`output_language`
- 生成結果に `prefix` / `suffix` を付与
- `negative_supported` が true かつ negative が空でなければ、ネガティブも別ファイル `*_neg.txt` に出力
- 文字数・単語数を計測し、`max_length` 超過ならUIで警告

---

## 5. 機能仕様

### 5.1 ざっくり生成
- 入力:日本語の自由指示、対象モデル(複数選択可)、出力フォルダ、生成本数、「おまかせ度」(0〜100)
- 処理:指示 → 中間表現(本数ぶん。バリエーションを付ける)→ 各対象モデル向けにレンダリング
- おまかせ度はtemperatureと「指示にない要素をどこまで補うか」の指示文に反映
- **保存前にプレビューを表示し、編集してから保存できる**
- 保存名:`(頭)_(通し番号 4 桁)_(モデル).txt`(例 `muse_0013_Krea2.txt`)。番号は、保存先のフォルダと `.muse` にある同じ頭のいちばん大きい番号の続きから、保存するものに一覧の順で付ける。名前の順に並べると作った順になる(2026-11-19 変更。前は `(頭)_(モデル)_(バリエーション番号).txt` で、生成と保存を繰り返すと `_2` `_3` が付いて順が入り乱れた)

### 5.2 抽出
- 入力:複数のプロンプトファイル(フォルダ指定/複数選択)、抽出する要素(チェックボックス:pose, expression, outfit, background, lighting, camera, style, motion…)、出力フォルダ
- 処理:**1ファイルずつ**LLMで中間表現に分解 → 選択された要素だけ取り出す
- 出力形式(選択式):
  - 要素ごとに1ファイル(例:`元名_extract_pose.txt`)
  - ワイルドカード形式:要素ごとに全ファイル分を1行1項目でまとめる(`pose.txt`)。完全一致の重複は除去
  - パーツライブラリに登録
- ComfyUIで生成したPNGのメタデータ(テキストチャンク内のプロンプト)も入力として読めるようにする(フェーズ5)

### 5.3 加工
| 機能 | 内容 |
|---|---|
| 盛る | 中間表現の空いた要素・薄い要素を補って詳細にする |
| 英訳 | 日本語の要素を英語に。元の日本語版は `*_ja.txt` として残す(設定でON/OFF) |
| モデル変換 | 既存ファイル → 中間表現 → 別モデルのプロファイルでレンダリング |
| 画像→動画 | 画像用の中間表現に motion / camera_motion / timeline を追加し、動画プロファイルでレンダリング。動画の秒数を入力でき、秒数に応じて動きの量を調整 |
| ネガティブ生成 | ポジティブ内容に合ったネガティブを作る(対応モデルのみ) |

- すべて**変更前と変更後を並べて表示(差分表示)し、確認してから保存**
- **削除する要素**(取り出すの反対、2026-11-19 利用者の希望):「取り出す要素」の横の囲みでチェックした要素のところだけを、元の文から LLM に消させる(言い回し・タグ・重み・記法は元のまま。組み直さない。指示文は `remove_system` / `remove_user`、温度 0.2)。分解した要素の語があれば消すところの手がかりとして渡す。削除だけなら分解は頼まない。表の「削除したあと」の列で直してから、`元名_without_background_pose.txt` に 1 ファイルずつ書き出す(空なら書かない。上の出力形式のチェックとは別に、削除する要素があれば必ず書く)。取り出す要素と同時に選べる

### 5.4 ストーリーモード
3段階で処理する。**段階ごとに別リクエストにする**(小さいモデルで1回にまとめると後半が崩れるため)。

**① 解釈**(task: `story_plan`)
- 入力:ざっくり指示、シーン数(任意)、テンポ指定(任意:「起承転結」「クライマックス多め」など)、おまかせ度
- オプション「AIに質問させる」:ONならLLMが確認質問を最大3問返す → ユーザーが回答またはスキップ

**② 設定シートと構成案**(task: `story_plan`、JSONで返させる)
```json
{
  "title": "勇者と魔王",
  "world": "dark fantasy, oil painting style, dusk tones",
  "common_suffix": "highly detailed, cinematic composition",
  "characters": [
    {"id": "hero", "name": "勇者", "look": "silver-haired young man, blue cape, scarred armor, one-handed sword"},
    {"id": "demon_lord", "name": "魔王", "look": "black horns, red eyes, jet-black robe"}
  ],
  "scenes": [
    {"no": 1, "title": "旅立ち", "summary": "勇者が村を出る", "characters": ["hero"]},
    {"no": 2, "title": "魔王城", "summary": "魔王城を見上げる", "characters": ["hero"]}
  ]
}
```
- **UIで編集可能にする**:キャラの見た目の文、シーンの追加・削除・並べ替え・内容修正
- 設定シートは保存・読み込み可能(「この勇者で別の話」用)。保存先:`library/characters/`、`library/stories/`

**③ 各シーン生成**(task: `render`、シーンごとに1リクエスト)
- 各シーンの中間表現を作る際、登場キャラの `look` を**毎回そのまま差し込む**(見た目の一貫性のため)
- `world` と `common_suffix` も全シーンに適用
- 出力:`001_旅立ち.txt`、`002_魔王城.txt` …(連番+シーン名。ファイル名に使えない文字は置換)
- オプション:
  - 動画用プロンプトもペアで出力(`001_旅立ち_video.txt`)。前シーン末尾の動き→次シーン冒頭のつながりを意識した記述にする
  - バリエーション展開:同じ構成で「昼版/夜版」「アニメ調/実写調」などを別サブフォルダに一括出力

実装で決めたこと(2026-11-19):
- look は LLM に任せず、シーンの中間表現の `subject` に毎回そのまま入れる(日本語の名前は英語のプロンプトに混ぜない)。レンダリングでは「この言葉はそのまま残す」と指示し、それでも抜けたら頭に足す(`renderer.ensure_verbatim`)。`world` は `style` に、`common_suffix` は末尾に、決まった形で付ける
- 動画ペアは `video/001_旅立ち_video.txt`(5.9 の置き場所)。中間表現は画像と共通で、動き(前のシーンから続き、次のシーンにつながる)だけ足す。look と world は変えさせない
- バリエーションは 1 行 1 つ「名前: 足す指示」(例 `夜版: night, moonlight`)。名前のサブフォルダに出す。指示は中間表現の `extra` に入れる
- 出力フォルダは空ならフォルダ欄のフォルダの下の「(タイトル)」。作ったときの設定シートを `.muse/story_sheet.json` に残す
- 読み込んだキャラ(library/characters)は、構成案を作るときに「このまま使う」として渡し、LLM の返事に同じキャラがあっても読み込んだ look を使う
- 「保存して生成する」で、保存したシーンをそのまま ComfyUI で生成できる(5.10)

### 5.5 パーツライブラリ
- カテゴリ別(pose, expression, outfit, background, lighting, camera, style, characters…)に項目を保存
- 保存形式:カテゴリごとのJSON(項目ごとにテキスト・元ファイル・登録日・お気に入りフラグ)
- 機能:一覧・検索・削除・お気に入り/ワイルドカード形式(`.txt`)で書き出し/**ミックス生成**:カテゴリごとに「ランダム/指定」を選び、組み合わせて中間表現を作りレンダリング(N本)

### 5.6 プロファイル管理画面
- モデルプロファイルとLLMプロファイルの一覧・編集・複製・削除(JSONを直接編集するフォーム、またはテキスト編集でもよい)
- **プロファイル自動作成**:公式のプロンプトガイドや作例のテキストを貼り付け → LLMがモデルプロファイルの下書きJSONを作る → ユーザーが確認して保存
- **お手本登録**:生成結果画面に「このプロンプトをお手本に追加」ボタン → 該当プロファイルの `example_prompts` に追記(上限数を超えたら古い方から削除するか確認)

### 5.7 LLM比較テスト
- 同じ入力を選択した複数のLLMプロファイルで実行し、結果と所要時間を並べて表示

### 5.8 画像からプロンプト(フェーズ5)
- `supports_vision: true` のLLMに画像(JPG/PNG)を送り、中間表現を作る → 任意のモデルでレンダリング

### フェーズ5 で決めたこと(2026-11-19)
- **ライブラリ**:`library/parts/(カテゴリ).json`。カテゴリ名が要素名(pose など)ならミックスでその要素に、`characters` は subject に、それ以外は extra に入れる。ミックスは LLM を使わずに組み合わせ(カテゴリごとに「使わない / ランダム / ★からランダム / 指定」)、同じ組み合わせはなるべく重ねない。各モデル向けのレンダリング・プレビュー・保存は「ざっくり生成」のタブで行う(ファイル名の頭は `mix`)
- **プロファイル管理**:JSON をそのまま直す。保存の前に形と要素名を確かめる。削除はごみ箱へ。保存するとほかのタブのモデルの一覧にすぐ反映。「ガイドから下書き...」は task `profile_draft`、知らない要素名は落とす。保存は利用者が確かめてから。LLM プロファイルは「使えるモデルから追加...」で LLM サーバーの一覧 (LM Studio の /api/v0/models、なければ /v1/models) から選んで作れる。種類が vlm なら supports_vision、Gemma は merge_user、文脈長は 8192 まで。名前は同じサーバーの既存プロファイルの「@…」を使う。埋め込み用のモデルは外す
- **お手本登録**:ざっくり生成・加工・ストーリーの結果に「お手本に追加」。上限は `settings.json` の `example_limit`(既定 5)。超えるときは「古いほうから消して足すか」を聞く。同じ文は足さない
- **翻訳 (AI)**:上の「翻訳 (AI)」グループの「翻訳する」がオンのあいだ、プレビューに出しているファイルの訳を右下の小窓に出す(見るだけ。ファイルは変えない)。PromptWordCrafter と同じ部品(`gencommon.translation`)で、サーバーURL はエンハンサー・読み取りと共有、モデルは「翻訳の設定」で別に選ぶ。オン/オフ・モデル・訳す向きは `ui_state.json` に保存
- **LLM 比較**:ざっくり生成と同じ処理(指示 → 中間表現 → 対象モデル向け)を、選んだ LLM プロファイルそれぞれに 1 件ずつ順に送り、時間(中間表現 / 書く)・語数・結果を並べる
- **画像から**:画像は長い辺 1024 の JPEG にして送る。保存名は `(画像の名前)_img2prompt_(モデル).txt`(保存先は空ならフォルダ欄のフォルダ)
- **PNG のメタデータ**:抽出・加工の「ファイルを選んで足す」で .png も選べる。ComfyUI の `prompt`(API 形式のグラフ。Negative のタイトルか KSampler の negative につながるノードは除き、いちばん長い文)、A1111 の `parameters`(Negative prompt: より前)を読む

### 5.9 PromptWordCrafter(PWC)との連携
画像・動画の生成は MuseFactory では行わず、PWC に任せる。流れ:
**MuseFactory でプロンプトファイルを作る → PWC がそのフォルダを開く(開いていれば読み直す)→ PWC で生成、または PWC から PrismLab に送って生成。**

- 保存後に「PWC で開く」(保存したフォルダを開き、作ったファイルを選ぶ)。設定で「保存したら自動で PWC に知らせる」を選べる
- 送り方は PWC → PrismLab と同じ:PWC の 2 重起動防止のパイプ(`PromptWordCrafter-(ユーザー名)`)に 1 行の JSON を書く
  `{"cmd": "open", "from": "MuseFactory", "folder": "...", "select": ["...", "..."]}`
  PWC が動いていなければ JSON を一時ファイルに書いて `PromptWordCrafter.exe --open (JSONのパス)`(開発中は `main.py`)で起動する
- PWC 側の変更(このリポジトリの外):パイプで JSON を受け取り、フォルダを開く/読み直す・ファイルを選ぶ。従来の `show` も受け付ける。未保存の編集があれば PWC の通常どおり保存確認を出す。PWC の README にも書く
- PWC は「テキストファイル 1 つ = プロンプト 1 本」で、フォルダの `.txt` をまとめて生成できる。生成に使わない副産物(中間表現 `.json`、`*_neg.txt`、`*_ja.txt`、動画ペア)が同じフォルダに並ぶと、まとめて生成に混ざる。そこで(2026-11-19 決定):
  - 出力フォルダ直下には生成に使うプロンプト `.txt` だけを置く
  - 中間表現 `.json`・`*_neg.txt`・`*_ja.txt` は `.muse/` サブフォルダに同じ名前の元で置く(PWC はサブフォルダ込みの生成でも `.` で始まるフォルダを飛ばす)
  - ストーリーの動画ペア(`*_video.txt`)は `video/` サブフォルダに置く(動画用ワークフローで別に生成するため)
  - 以下の章の「同名の .json」「`*_neg.txt`」「`*_ja.txt`」「`001_旅立ち_video.txt`」は、この置き場所に読み替える

### 5.10 画像・動画の生成(ComfyUI)(2026-11-19 追加)
最初は PWC に任せる方針だったが、MuseFactory の中でも生成できるようにした。PWC・ImageMimicArt と同じ共通部品 **GenCommon** を使い、J-Rate の `docs\generation-standard.md` に従う(MuseFactory はこの決まりを持つ 4 つ目のアプリ)。

- **テキストファイル 1 つ = プロンプト 1 本**(PWC と同じ)。生成は**保存したファイルから**始める(保存前のプレビューからは生成しない。記録がファイルに結び付き、「生成時の設定」「この設定で生成する」「旧」の印が使えるように)
- 入口:上の「生成 (ComfyUI)」グループ(生成する | 生成ジョブ | 生成の設定 | 図で設定)、ファイル一覧・フォルダツリーの右クリック、ざっくり生成・加工のタブの「保存して生成する...」
- 結果:テキストのフォルダの中の `生成結果` フォルダに `(元の名前)_t2i.png` / `(元の名前)_video.mp4`(PWC と同じなので、同じフォルダを PWC で開いても結果が出る)。プレビューの下に PWC と同じ生成結果の帯を出す
- 接続先・ワークフロー・エンハンサーなどの登録は PWC などと共有(`%APPDATA%\GenCommon\`)。セット・記録・ジョブは `%APPDATA%\MuseFactory\`
- ネガティブ(`.muse` の `*_neg.txt`)は当面生成に使わない(GenCommon は Negative のノードに入れない)。必要になったら GenCommon に目印を足す
- 「PWC で開く」は残す。PrismLab へは MuseFactory から直接送れる(右クリック・「連携」グループの「PrismLab に送る」。PWC と同じ送り方・同じ送り先)
- 生成結果の帯・PrismLab に送る・テキストの読み方は PWC の `results.py` / `results_view.py` / `prismlab_link.py` / `text_io.py` を `app\gen\` に写したもの(generation-standard 11 章: 結果の結び付けはアプリに残す)。直すときは PWC 側とそろえる

---

## 6. 共通仕様
- **上書き禁止**:同名ファイルがあれば `_2`, `_3` を付ける
- 命名ルール:`元ファイル名_処理名_要素名.txt`(処理名は extract / enhance / en / conv_<モデル名> など)
- 文字コード:UTF-8(BOMなし)
- ログ:`logs/` に日付ごと。LLMへのリクエスト(要約)・応答時間・エラーを記録
- 接続テストボタン(LLMプロファイルごと)
- 設定・ウィンドウ位置などは `config/` に保存

---

## 7. 実装フェーズ

各フェーズ完了時に「確認手順」を実行し、結果を報告してから次へ。コミットはユーザーが `git diff` を確認してから行うので、**勝手にコミットしないこと**。

### フェーズ1:基盤
- PromptWordCrafter のソースを確認し、GUIフレームワークと画面構成を報告(この時点で計画を提示して承認を得る)
- ディレクトリ構成、設定・プロファイル読み込み
- `llm_client.py`(`<think>`除去、JSON修復・再試行を含む)、接続テスト
- `router.py`(フォールバック、ComfyUIのqueue確認)、`job_queue.py`
- メイン画面の骨組み(PromptWordCrafter踏襲)、ステータス欄
- **確認手順**:接続テストが成功/失敗を正しく表示する。存在しない接続先を先頭にしても次の候補に切り替わる。`<think>`除去とJSON修復のユニットテストが通る

### フェーズ2:中間表現+ざっくり生成
- `intermediate.py`、`renderer.py`、初期モデルプロファイル(雛形)
- 5.1 ざっくり生成(プレビュー・編集・保存)、共通の命名・上書き禁止
- **確認手順**:1つの指示から2モデル×2本のファイルが作られ、中間表現JSONも保存される。`skip_elements` と `negative_supported` が反映される

- 5.9 PWC 連携(「PWC で開く」と PWC 側の受け取り)
- **確認手順(追加)**:保存後に「PWC で開く」で、PWC が動いていてもいなくても、そのフォルダが開いて作ったファイルが選ばれる

### フェーズ3:抽出・加工
- 5.2 抽出(要素ごと/ワイルドカード形式)、5.3 加工(差分表示つき)
- 一括処理時のモデル別並べ替え、進捗・キャンセル
- **確認手順**:3ファイルからポーズと表情を抽出し、両形式で出力できる。途中キャンセルが効く。英訳時に日本語版も残る

### フェーズ3.5:生成の取り込み(2026-11-19 追加)
- 5.10 生成(GenCommon)、生成結果の帯、PrismLab に送る、「保存して生成する」
- **確認手順**:保存したファイルから生成でき、結果がプレビューの下の帯に出る。同じフォルダを PWC で開いても結果が見える。PrismLab に送れる

### フェーズ4:ストーリーモード
- 5.4 の①②③、設定シート編集UI、保存・読み込み、動画ペア出力、バリエーション展開
- **確認手順**:「ファンタジーで勇者が魔王と戦ってる一連のやつ」から6シーン前後の構成案ができ、編集後に連番ファイルが出力される。全シーンにキャラの `look` が含まれる

### フェーズ5:ライブラリ・管理・拡張
- 5.5 パーツライブラリ+ミックス生成、5.6 プロファイル管理・自動作成・お手本登録、5.7 LLM比較、5.8 画像からプロンプト、PNGメタデータ読み込み
- **確認手順**:各機能を一通り操作して動作を報告

---

## 7.5 決定事項(2026-11-19 利用者と確認)
- GUI は PySide6。PWC の画面構成(上にフォルダ欄・機能ごとのグループ、下に「ツリー|ファイル一覧|作業欄」、ステータスバー)を踏襲し、作業欄は機能ごとのタブにする。配色は PWC と同じ明るい配色(J-Rate ui-standard の暗い配色は採らない)
- 設定は SPEC どおりアプリのフォルダの `config/`(ui-standard の `%APPDATA%` は採らない)
- LLM プロファイル・モデルプロファイルは GenCommon の共有設定(LLM の URL・エンハンサー)とは別に持つ。LLM の通信も自前の `llm_client.py`
- タスク割り当てに `generate`(指示→中間表現)、`negative`、`profile_draft` を足す。設定にないタスクは `render` の割り当てを使う
- 出力が日本語のときは `max_length` を文字数の目安として扱う
- LLM は当面 LLM 機の 9B(`qwen/qwen3.5-9b`)だけ。すべてのタスクを 9B に割り当てる(14B のプロファイルは置かない。PC1/PC2 の LM Studio を使うときにプロファイルと `comfyui_hosts` を足す)。画像を読むのは `gemma-4-e4b-uncensored-hauhaucs-aggressive`
- 使うモデルは接続テストの画面の「モデルを変える...」で、接続先のモデル一覧から選び直せる(プロファイルのファイルに保存)
- Qwen 3.5 9B は画像も見られる (vlm) ので image_to_prompt も Qwen 9B を先にし、Gemma Vision は予備にする。モデルの入れ替えがなくなり、1 枚 3〜9 秒 (Gemma に入れ替えると 80 秒ほど)
- タスク割り当ては接続テストの画面の「割り当てを変える...」(行のダブルクリックでも可)で、使う LLM と試す順を選ぶ。「ほかのタスクも全部これにする」で画像から以外をまとめて変えられる。image_to_prompt は画像を見られるプロファイルだけ選べる。config/settings.json に保存し、すぐ効く

## 7.6 EXE
- 作り方: リポジトリの直下で `py -3.11 -m PyInstaller --noconfirm MuseFactory.spec`(`..\GenCommon` を取り込む)。出来た `dist\MuseFactory.exe` を `Desktop\Application` に写す(写す前に MuseFactory を閉じる)
- EXE のデータは「EXE のあるフォルダ\MuseFactory」(config / profiles / library / logs)。Application にはほかのアプリも並ぶのでまとめる。開発中(`py main.py`)はリポジトリの直下を使うので、データは別々
- 初めて起動したとき、EXE に入れた既定のプロファイルを写す(あるファイルは上書きしない)。LLM への指示文(prompts/*.txt)は EXE の中のものを使う
- アイコンは `assets/icon.ico`

## 8. 実装上の注意(LLM関連)
- 小さいモデル(9B)前提:**JSONスキーマは浅く、1リクエスト1仕事**にする
- temperatureの目安:抽出・翻訳 0.1〜0.3/各シーンのプロンプト化 0.5〜0.7/盛る・構成案・バリエーション 0.8前後(プロファイルの値を優先し、タスク側で上書き可能に)
- 複数ファイルはまとめて渡さず**1ファイルずつ**処理して結果を合体させる
- コンテキスト長は 4k〜8k を想定してプロンプトを組む(8GB VRAMでモデル本体をGPUに収めるため)
- LLMに送るシステムプロンプト類は、コードに直書きせず `app/core/tasks/prompts/` 等のテキストファイルに分けて、後から調整しやすくする
