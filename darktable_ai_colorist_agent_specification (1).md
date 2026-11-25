# TECHNICAL SPECIFICATION: DARKTABLE AGENTIC COLORIST PLUGIN (ANTIGRAVITY CLI + HUMAN-IN-THE-LOOP)

## 1. Mục tiêu dự án (Project Overview)

Xây dựng một plugin tích hợp trực tiếp vào chế độ **Darkroom** của phần mềm **Darktable**, vận hành theo mô hình **Closed-Loop AI Agent kết hợp Human-in-the-Loop (HITL)** và sử dụng **Antigravity CLI** làm bộ não thực thi thị giác:

* **Không dùng AI vẽ lại pixel (Non-generative):** Mọi thay đổi đều là tham số toán học (non-destructive) đẩy trực tiếp vào các thanh trượt của Darktable, giữ nguyên 100% độ phân giải và chi tiết gốc của file ảnh.

* **Thực thi qua Antigravity CLI (Zero-API-Key Setup):** Không cần cấu hình API key rời hay bật thêm local server phụ; Python Orchestrator giao tiếp trực tiếp với công cụ dòng lệnh `antigravity` đang cài sẵn trên máy để tận dụng tài khoản và model đa phương thức tích hợp.

* **Vòng lặp kép (Dual-Loop Architecture):**

  1. **Inner Loop (AI Auto-Verify):** AI tự quan sát ảnh + biểu đồ Histogram thực tế, xuất bộ tham số, render lại ảnh preview qua `darktable-cli` và tự tinh chỉnh (tối đa $N$ bước) cho đến khi đạt chuẩn kỹ thuật.

  2. **Outer Loop (Human Review & Feedback):** Plugin áp kết quả lên khung hình trung tâm (canvas) của Darkroom để người dùng xem trực tiếp với profile màu chuẩn của màn hình. Người dùng có thể bấm **Approve (Chốt)** hoặc nhập **Feedback Prompt** (VD: *"ảnh hơi vàng, cân bằng trắng lại, da hồng hơn"*) để AI Agent tiếp tục tinh chỉnh dựa trên trạng thái hiện tại.

## 2. Kiến trúc hệ thống & Luồng hoạt động (System Architecture & State Machine)

Hệ thống tách làm 2 tầng giao tiếp qua CLI và File System để đảm bảo không làm treo giao diện UI của Darktable:

```
[1. INITIAL STATE - Darktable Darkroom UI (Lua)]
   User chọn Model Antigravity + nhập Style Prompt ban đầu -> Bấm [Run AI Agent]
        │
        ▼
[2. INNER LOOP - Python Agent Orchestrator (dt_ai_agent.py)]
   ┌────────────────────────────────────────────────────────────────────┐
   │  (a) Observe: Tính Histogram (OpenCV) -> Ghi `task_input.json`     │
   │  (b) Reason: Gọi `antigravity` CLI (non-interactive) đọc           │
   │              `preview.jpg` + `task_input.json` -> `output.json`    │
   │  (c) Act: Đọc JSON tham số -> Cập nhật file XMP tạm                │
   │  (d) Verify: Gọi `darktable-cli` render ảnh preview mới            │
   │      -> Nếu `is_satisfied == true` hoặc đạt Max -> Thoát           │
   └────────────────────────────────────────────────────────────────────┘
        │
        ▼
[3. PREVIEW & REVIEW STATE - Darktable Canvas (Lua)]
   - Lua lưu trạng thái gốc (để User so sánh Before / After trên canvas chính).
   - Lua áp bộ thông số mới nhất lên UI (`dt.gui.action`) -> Ảnh cập nhật ngay lập tức.
   - Panel UI mở khóa nhóm nút duyệt: [Approve] và ô nhập [Refinement Feedback].
        │
        ├──► Nhánh A: User hài lòng -> Bấm [Approve]
        │       └── Giữ nguyên History Stack, xóa file session tạm, kết thúc phiên.
        │
        └──► Nhánh B: User chưa ưng -> Nhập Feedback ("ảnh hơi vàng, da hồng hơn...")
                └── Bấm [Refine with Feedback] -> Quay lại bước [2] kèm session cũ + lời nhắc mới.

```

## 3. Yêu cầu chức năng chi tiết (Functional Requirements)

### 3.1. Giao diện người dùng (Darktable Lua Plugin - `ai_color_agent.lua`)

Đăng ký một panel mới tại vị trí `DT_UI_CONTAINER_PANEL_RIGHT_CENTER` trong view `darkroom` gồm 2 khu vực rõ ràng:

#### Khu vực 1: Cấu hình & Chạy lần đầu (Initial Setup)

1. **Model Selector (`combobox`):** Hiển thị danh sách profile model của Antigravity CLI (`antigravity/default`, `antigravity/gemini-pro`, `antigravity/claude-sonnet`, v.v.).

2. **Refresh Models (`button`):** Gọi Python với cờ `--list-models` để kiểm tra trạng thái cài đặt `antigravity` CLI và nạp danh sách lựa chọn vào `combobox`.

3. **Initial Style Prompt (`entry`):** Ô nhập text cho phép người dùng mô tả phong cách màu sắc/ánh sáng ban đầu (VD: *"Ảnh trong trẻo Nhật Bản, sáng nhẹ, da hồng hào, bầu trời xanh dịu"*).

4. **Max Iterations (`combobox` / `slider`):** Số vòng lặp tự kiểm tra tối đa của AI ở mỗi lượt (mặc định: `3`).

5. **Run AI Agent (`button`):** Khởi chạy phiên làm việc mới (Reset session history, lưu trạng thái gốc trước khi áp thông số).

#### Khu vực 2: Xem trước & Phản hồi (Preview & Human-in-the-Loop Controls)

*(Chỉ kích hoạt / hiển thị rõ sau khi có kết quả Preview từ AI)*

1. **AI Critique (`label`):** Hiển thị ngắn gọn nhận xét/tóm tắt chỉnh sửa của AI ở bước vừa rồi.

2. **Toggle Before / After (`button`):** Bật/tắt nhanh trạng thái trước và sau khi AI chỉnh để người dùng soi trực tiếp sự thay đổi ngay trên canvas trung tâm của Darkroom.

3. **Refinement Prompt (`entry`):** Ô nhập yêu cầu sửa chi tiết dựa trên ảnh đang hiển thị (VD: *"ảnh hơi vàng, chỉnh màu trắng lại, da hồng hơn"*).

4. **Refine (`button`):** Xuất ảnh preview từ trạng thái đã chỉnh hiện tại, gửi kèm `session_id` và câu feedback mới sang Python để chạy tiếp vòng tinh chỉnh.

5. **Approve (`button`):** Xác nhận chốt kết quả hiện tại, dọn dẹp file session tạm và reset UI về trạng thái sẵn sàng cho ảnh tiếp theo.

### 3.2. Quản lý trạng thái phiên làm việc (Session State & Memory)

Để AI hiểu được các câu feedback như *"ảnh hơi vàng, da hồng hơn"* là điều chỉnh **tương đối so với bức ảnh hiện tại** (tránh reset làm mất kết quả của vòng trước), Python Agent duy trì file trạng thái `session_<image_id>.json` trong thư mục tạm:

* **Cấu trúc lưu trữ:**

  * `initial_prompt`: Phong cách gốc ban đầu.

  * `current_params`: Bộ thông số hiện tại đang áp dụng trên Darktable.

  * `conversation_history`: Lịch sử nhận xét của AI và các câu `refinement_feedback` của người dùng qua từng lượt.

* **Chỉ thị System Prompt ở chế độ `--refine`:**

  > *"Người dùng đang xem bức ảnh đính kèm (đã áp dụng bộ thông số `current_params`). Dựa vào phản hồi mới của người dùng: '{feedback_prompt}', hãy điều chỉnh tăng/giảm các tham số từ mức hiện tại để khắc phục đúng điểm người dùng chưa hài lòng mà vẫn giữ nguyên tinh thần của `initial_prompt`."*

### 3.3. Tự động nhận diện & Tích hợp Antigravity CLI (`--list-models` & `antigravity_bridge.py`)

Thay thế hoàn toàn việc gọi REST API (Gemini/Ollama/LM Studio) bằng cơ chế điều phối trực tiếp **Antigravity CLI**:

1. **Kiểm tra & Liệt kê Model (`--list-models`):**
   * Dùng `shutil.which("antigravity")` hoặc đọc đường dẫn thực thi `antigravity_bin` từ `config.json`.
   * Nếu tìm thấy, trả về danh sách các cấu hình model hỗ trợ qua CLI:
     * `antigravity/default` (Dùng model mặc định đang cấu hình trong Antigravity).
     * `antigravity/gemini-pro` (Chỉ định cờ `--model` sang Gemini Pro cho ảnh ánh sáng phức tạp).
     * `antigravity/gemini-flash` (Chỉ định cờ `--model` sang Gemini Flash cho tốc độ vòng lặp nhanh).
   * Nếu chưa cài đặt hoặc không tìm thấy trong `PATH`, trả về thông báo hướng dẫn cấu hình đường dẫn trong `config.json`.

2. **Cơ chế giao tiếp Non-Interactive qua Workspace Tạm (`antigravity_bridge.py`):**
   * Tạo thư mục làm việc tạm `work_dir` cho mỗi bước lặp, chứa:
     * `preview.jpg`: Ảnh preview hiện tại.
     * `task_input.json`: Đóng gói `initial_prompt`, `refinement_feedback`, `histogram_metrics`, `current_params` và `conversation_history`.
   * Gọi `subprocess.run(...)` thực thi lệnh `antigravity` ở chế độ non-interactive (không mở UI tương tác), truyền prompt yêu cầu đọc file `preview.jpg` và `task_input.json` trong `work_dir`, sau đó **bắt buộc ghi duy nhất 1 file `output_params.json`** tuân thủ đúng JSON Schema vào `work_dir`.
   * *Lưu ý triển khai cho AI Coding Agent:* Tự động kiểm tra `antigravity --help` khi cài đặt/chạy lần đầu để map đúng cú pháp flag của phiên bản Antigravity CLI trên máy (VD: cờ truyền prompt `-p` / `run`, cờ chọn `--model`), đồng thời có cơ chế fallback trích xuất block JSON từ `stdout` nếu CLI in trực tiếp ra terminal thay vì ghi file.

### 3.4. Phân tích thị giác & Định lượng Histogram (`image_analyzer.py`)

Để tránh hiện tượng AI bị "ảo giác" độ sáng qua ảnh nén JPEG, trước mỗi lần gọi Antigravity CLI, Python dùng `OpenCV` và `NumPy` trích xuất chỉ số thực tế của ảnh preview hiện tại và ghi vào `task_input.json`:

* Độ sáng trung bình kênh Luminance/Value ($0 - 255$).

* Tỷ lệ % pixel cháy sáng ($\text{Clipping Highlights} > 250$).

* Tỷ lệ % pixel bệt tối ($\text{Crushed Shadows} < 8$).

* Giá trị trung bình 3 kênh màu $(R_{\text{mean}}, G_{\text{mean}}, B_{\text{mean}})$ và tỷ lệ lệch kênh $R/B$, $G/B$ để AI nhận biết chính xác mức độ ám vàng/ám xanh.

### 3.5. Giao thức dữ liệu & Structured Output (`output_params.json`)

Ép buộc Antigravity CLI xuất ra file `output_params.json` đúng cấu trúc JSON chuẩn (không chứa markdown thừa):

```json
{
  "step": 1,
  "critique": "Ảnh hơi tối ở vùng midtones, ám vàng nhẹ. Đã nâng sáng +0.65 EV và cân bằng lại nhiệt độ màu.",
  "is_satisfied": false,
  "params": {
    "exposure": 0.65,
    "black_level": -0.002,
    "temperature_k": 5400,
    "tint": 0.04,
    "sigmoid_contrast": 1.15,
    "sigmoid_skew": -0.1,
    "global_vibrance": 12.0,
    "shadows_luminance": 10.0,
    "highlights_luminance": -15.0
  }
}
```

### 3.6. Cơ chế Render nội bộ & Áp thanh trượt UI

1. **Trong Inner Loop (Python - Intermediate Steps):**

   * Sử dụng module `xmp_modifier.py` (dùng thư viện `struct` của Python để pack các số `float` thành đúng cấu trúc binary C-struct hex của các module Darktable 4.6+: `exposure`, `temperature` / `channelmixerrgb`, `sigmoid`, `colorbalancergb`) ghi vào file `temp.xmp`.

   * Gọi `darktable-cli <raw_file> <temp_xmp> <next_preview.jpg>` chạy ngầm để xuất ảnh preview cho vòng lặp tiếp theo.

2. **Hiển thị Preview trên UI (Lua - Final Step của mỗi lượt):**

   * Lua nhận JSON kết quả từ Python và gọi API nội bộ `dt.gui.action(...)` để kéo trực tiếp các thanh trượt trên giao diện Darkroom (đảm bảo an toàn 100% với database và History Stack của Darktable):

     * `iop/exposure/exposure` $\rightarrow$ `exposure`

     * `iop/exposure/black` $\rightarrow$ `black_level`

     * `iop/temperature/temperature` $\rightarrow$ `temperature_k`

     * `iop/temperature/tint` $\rightarrow$ `tint`

     * `iop/sigmoid/contrast` $\rightarrow$ `sigmoid_contrast`

     * `iop/sigmoid/skew` $\rightarrow$ `sigmoid_skew`

     * `iop/colorbalancergb/global vibrance` $\rightarrow$ `global_vibrance / 100.0`

     * `iop/colorbalancergb/shadows luminance` $\rightarrow$ `shadows_luminance / 100.0`

     * `iop/colorbalancergb/highlights luminance` $\rightarrow$ `highlights_luminance / 100.0`

## 4. Cấu trúc mã nguồn & Giao thức CLI (Deliverables)

AI Coding Agent cần tạo ra bộ mã nguồn hoàn chỉnh theo cấu trúc thư mục sau:

```text
darktable-ai-colorist/
├── README.md                 # Hướng dẫn cài đặt chi tiết cho Windows & Linux, cấu hình antigravity CLI & luarc
├── requirements.txt          # opencv-python, numpy
├── config.example.json       # Mẫu cấu hình antigravity_bin, darktable_cli_path, max_inner_steps
├── lua/
│   └── ai_color_agent.lua    # UI Panel (Initial + Preview/Approve/Refine) & dt.gui.action mapper
└── python/
    ├── dt_ai_agent.py        # CLI Entrypoint hỗ trợ --list-models, --run, --refine, --approve
    ├── antigravity_bridge.py # Giao tiếp Antigravity CLI, truyền ảnh & parse output_params.json
    ├── session_manager.py    # Quản lý state JSON theo từng ảnh (current_params, feedback history)
    ├── image_analyzer.py     # Tính toán Histogram, clipping & tỷ lệ kênh màu R/G/B bằng OpenCV
    └── xmp_modifier.py       # Đọc/ghi binary struct hex chuẩn Darktable 4.6+ cho file .xmp
```

### Giao thức CLI chuẩn giữa Lua và Python (`dt_ai_agent.py`):

* **Quét danh sách model Antigravity:**

  ```bash
  python dt_ai_agent.py --list-models
  ```

* **Chạy lần đầu (Khởi tạo session mới):**

  ```bash
  python dt_ai_agent.py --run --session-id <img_id> --raw "<raw_path>" --preview "<jpg_path>" --model "<model>" --prompt "<style_prompt>"
  ```

* **Chạy tinh chỉnh theo phản hồi người dùng (Đọc tiếp session cũ):**

  ```bash
  python dt_ai_agent.py --refine --session-id <img_id> --raw "<raw_path>" --preview "<current_jpg_path>" --model "<model>" --feedback "<user_feedback>"
  ```

* **Chốt kết quả & Dọn dẹp session:**

  ```bash
  python dt_ai_agent.py --approve --session-id <img_id>
  ```

## 5. Yêu cầu phi chức năng (Non-Functional Requirements)

* **Xử lý lỗi (Error Handling):** Nếu không tìm thấy lệnh `antigravity` trong hệ thống, quá thời gian chờ (timeout), hoặc file `output_params.json` không hợp lệ, bắt lỗi rõ ràng và hiển thị thông báo trên Darktable qua `dt.print()`.

* **Dọn dẹp tài nguyên:** Tự động xóa toàn bộ thư mục tạm `work_dir`, file ảnh `.jpg` và `.xmp` trung gian sau khi hoàn tất hoặc khi bấm `--approve`.

* **Tương thích đa nền tảng:** Xử lý đúng đường dẫn file và escape ký tự đặc biệt (tiếng Việt có dấu, dấu ngoặc kép) khi truyền tham số qua dòng lệnh trên cả Windows và Linux/macOS.

## 6. Dev Analysis

### Chat trong Darkroom

Panel dùng một ô nhập và nút **Send** cho cả hai lượt trong mục 3.1. Tin nhắn đầu của phiên gọi `--run`. Các tin nhắn tiếp theo gọi `--refine` với cùng session ID và bản After mới nhất. Một lượt hiển thị một tin nhắn của người dùng và một phản hồi cuối của AI, kể cả khi AI thực hiện nhiều vòng lặp nội bộ. Transcript được giữ riêng theo ảnh trong lần mở Darktable hiện tại. Lượt mới nhất nằm sát ô nhập để có thể đọc ngay trong panel.

### Trạng thái xử lý và phục hồi

Trong lúc chờ Python, panel hiện **AI is processing…**, đăng ký công việc nền trong Darktable và khóa gửi trùng. Khi xử lý thành công hoặc lỗi, panel bỏ trạng thái chờ và mở lại các điều khiển. Nếu gửi lỗi, cuộc chat ghi thông báo và ô nhập khôi phục nội dung để có thể gửi lại. Kết quả được gắn với ảnh của lượt gửi. Khi chuyển ảnh trong lúc xử lý, có thể quay về ảnh đó và dùng **Show After** để áp preview.

### History stack và XMP

**Show Before / Show After** và **Approve** vẫn dùng quy trình XMP 5.6.2 đã kiểm thử. **Approve** chốt bản After, dọn phiên chỉnh màu và giữ transcript đang hiển thị. Tin nhắn tiếp theo mở phiên chỉnh màu mới từ trạng thái đã chốt. Thay đổi UI dùng các lệnh Python và giao thức XMP hiện hữu.

### Antigravity headless

Plugin yêu cầu công cụ `view_file` có sẵn trong Antigravity đọc preview JPEG và dữ liệu của lượt chỉnh màu. Với CLI có hỗ trợ, plugin lấy JSON qua stdout và truyền schema bằng `--json-schema`. Quy tắc chặn MCP chỉ có hiệu lực trong tiến trình Antigravity của lượt chỉnh màu. Cấu hình quyền Antigravity của người dùng giữ nguyên.

### Model và ngữ cảnh chỉnh ảnh

**Refresh Models** hợp nhất danh sách Antigravity, gồm Claude nếu tài khoản cung cấp, với các model GPT hỗ trợ ảnh từ Codex CLI. Nhánh `chatgpt/…` gửi JPEG đính kèm và nhận kết quả theo cùng schema chỉnh màu qua phiên đăng nhập ChatGPT của Codex CLI.

`darktable_context` gửi cho model danh sách module được ghi trong XMP, trạng thái bật/tắt, thông tin mask và các thiết lập liên quan như bù sáng, hệ số White Balance, cách xử lý màu của Sigmoid và các giá trị Color Balance RGB. Context đi kèm histogram, `current_params` và lịch sử hội thoại. Agent đọc lại context từ history của preview ở mỗi vòng để biết ảnh đang có những chỉnh sửa nào và tinh chỉnh tương đối từ đó. Các trường ngữ cảnh chỉ dành cho đọc. Kết quả chỉnh vẫn phải nằm trong `allowed_params`.
