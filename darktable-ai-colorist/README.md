# Darktable AI Colorist

Plugin Darkroom dành cho **Darktable 5.6.2**. Antigravity quan sát preview JPEG và histogram thực tế, đề xuất các giá trị thanh trượt, rồi dùng bản render mới để đánh giá lại. Darktable áp dụng các chỉnh sửa qua history stack. Tệp ảnh nguồn giữ nguyên.

## Cài đặt trên Windows

1. Cài Darktable **5.6.2** và Python 3.10 trở lên. Mở Darktable và chọn chế độ ghi XMP **on import** trong Preferences → Storage. Plugin cần quyền ghi sidecar cạnh ảnh.
2. Cài [Antigravity CLI](https://antigravity.google/docs/cli-getting-started/) và chạy `agy` một lần để đăng nhập. `agy models` kiểm tra danh sách model của tài khoản.
3. Dùng PowerShell trong thư mục `darktable-ai-colorist` để tạo môi trường và kiểm tra kết nối tới Antigravity.

   ```powershell
   python -m venv .venv
   .\.venv\Scripts\python.exe -m pip install -r requirements.txt
   Copy-Item config.example.json config.json
   .\.venv\Scripts\python.exe python\dt_ai_agent.py --list-models
   ```

   Nếu `python` mở Microsoft Store, dùng đường dẫn tới `python.exe` đã cài để tạo môi trường.

4. Thêm một dòng cuối tệp `%LOCALAPPDATA%\darktable\luarc`. Nếu Darktable chạy với `--configdir`, dùng `luarc` trong thư mục đó.

   ```lua
   dofile([[C:/Projects/darktable-ai-colorist/lua/ai_color_agent.lua]])
   ```

5. Khởi động lại Darktable. Trong Preferences → Lua options, đặt **AI Colorist Python** thành đường dẫn tuyệt đối tới `.venv\Scripts\python.exe`. Dùng dấu gạch chéo `/` hoặc escape `\\` cho một dấu gạch chéo ngược trong JSON.

## Cài đặt trên Linux và macOS

1. Cài Darktable **5.6.2** có Lua và `darktable-cli`, Python 3.10 trở lên, cùng Antigravity CLI. Đăng nhập bằng `agy`. Bật ghi XMP **on import** trong Darktable.
2. Tạo môi trường và kiểm tra kết nối tới Antigravity từ thư mục dự án.

   ```sh
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements.txt
   cp config.example.json config.json
   .venv/bin/python python/dt_ai_agent.py --list-models
   ```

3. Thêm `dofile([[/absolute/path/darktable-ai-colorist/lua/ai_color_agent.lua]])` vào `${XDG_CONFIG_HOME:-$HOME/.config}/darktable/luarc` trên Linux hoặc tệp `luarc` trong thư mục cấu hình Darktable trên macOS. Khởi động lại và đặt **AI Colorist Python** thành đường dẫn tuyệt đối tới `.venv/bin/python`.

## Cấu hình CLI

| Khóa | Mặc định và cách dùng |
| --- | --- |
| `antigravity_bin` | Tìm `antigravity` hoặc `agy` trong PATH. Điền đường dẫn tuyệt đối nếu GUI không thấy PATH của terminal. |
| `codex_bin` | Tìm Codex CLI trong PATH hoặc trong bản cài Codex trên Windows. Điền đường dẫn tuyệt đối nếu cài ở vị trí khác. |
| `darktable_cli_path` | Tìm trong PATH hoặc thư mục cài đặt chuẩn trên Windows/macOS. Điền đường dẫn nếu cài ở vị trí khác. |
| `rawspeed_cameras` | Tìm `share/darktable/rawspeed/cameras.xml` trong bản cài. Điền đường dẫn đến tệp của Darktable 5.6.2 nếu cần. |
| `max_inner_steps` | 3. Cờ `--max-steps` hoặc lựa chọn trong panel có thể ghi đè. |
| `preview_size` | Cạnh dài tối đa 1600 pixel, xuất preview sRGB. |
| `antigravity_timeout_seconds` | 240 mỗi lần gọi model, dùng cho cả Antigravity và Codex. |
| `darktable_timeout_seconds` | 180 mỗi lần render. |
| `models` | Ánh xạ tên profile sang slug model, ví dụ `{"gemini-flash": "slug-tu-agy-models"}`. |

**Refresh Models** lấy các model Gemini và Claude mà tài khoản Antigravity cung cấp, cùng các model GPT có đầu vào ảnh mà Codex CLI liệt kê. Mục `chatgpt/…` dùng phiên đăng nhập ChatGPT trong Codex CLI. Mục `antigravity/…` dùng phiên đăng nhập `agy`.

Trong mỗi vòng lặp, nhà cung cấp đã chọn nhận preview JPEG, histogram, prompt, lịch sử nhận xét và tóm tắt các thông số chỉnh ảnh. Tóm tắt gồm giá trị slider hiện tại, các module được ghi trong history XMP và trạng thái như chế độ Exposure, White Balance, Sigmoid, Color Balance RGB và mask. Plugin tạo tóm tắt từ history dùng để render preview và cập nhật sau mỗi lần chỉnh.

## Dùng trong Darkroom

1. Mở ảnh trong Darkroom. Dùng **Refresh Models** để nạp các model, chọn model và số vòng lặp tối đa. Mặc định là 3.
2. Nhập mô tả phong cách trong ô chat và bấm **Send**. Trong lúc chạy, panel hiện **AI is processing…**. Darktable chuyển qua Lighttable trong chốc lát để ghi snapshot XMP. Render và phân tích chạy trong tiến trình riêng.
3. Phản hồi xuất hiện ngay dưới tin nhắn của bạn. Lượt mới nhất nằm sát ô nhập. Gửi tiếp trong cùng ô để AI tinh chỉnh bản After theo phản hồi của bạn.
4. Dùng **Show Before** hoặc **Show After** để so sánh trong Darkroom. Mỗi ảnh giữ cuộc chat riêng trong lần mở Darktable hiện tại.
5. Bấm **Approve** để chốt bản After và dọn phiên chỉnh màu. Cuộc chat vẫn hiển thị. Tin nhắn kế tiếp bắt đầu phiên chỉnh màu mới.

Canvas nạp XMP đã render bằng API `image:apply_sidecar()` của Darktable. Các thanh trượt phản ánh history stack vừa nạp. Cách này giữ cùng trạng thái module, blend, mask và thứ tự pipeline với preview. Plugin chuyển qua Lighttable trong lúc nạp history để tránh treo giao diện trên 5.6.2. Plugin đọc Kelvin và tint hiện tại qua `dt.gui.action`.

## Dùng từ terminal

```sh
python python/dt_ai_agent.py --list-models
python python/dt_ai_agent.py --run --session-id 42 --raw "IMG_0042.CR3" --preview "preview.jpg" --model antigravity/default --prompt "Ảnh trong trẻo, tông ấm nhẹ" --output-xmp "grade.xmp"
python python/dt_ai_agent.py --refine --session-id 42 --raw "IMG_0042.CR3" --feedback "Giảm ám vàng" --output-xmp "grade-refined.xmp"
python python/dt_ai_agent.py --approve --session-id 42
```

Bỏ `--preview` để render preview đầu tiên từ XMP hiện hành. Dùng `--xmp` để chỉ định sidecar nguồn, ví dụ khi chỉnh virtual copy. Nếu dùng `--preview`, ảnh đó phải khớp với history được dùng làm điểm bắt đầu. `--output-xmp` xuất XMP sang đường dẫn riêng. Các lệnh thành công trả JSON trên stdout.

## XMP và đơn vị

| Module | Phiên bản tham số | Kích thước | C-struct little-endian |
| --- | ---: | ---: | --- |
| `exposure` | 7 | 28 byte | `<iffffii` |
| `temperature` | 4 | 20 byte | `<ffffi` |
| `sigmoid` | 3 | 56 byte | `<ffffi8fi` |
| `colorbalancergb` | 5 | 132 byte | `<32fi` |

Tint là giá trị tuyệt đối của Darktable, trung tính tại `1.0`, phạm vi `0.135–2.326`. White Balance chuyển đổi Kelvin/tint thành hệ số theo ma trận cảm biến của DNG hoặc RawSpeed. Với RAW thiếu ma trận nhận diện được, model vẫn có thể chỉnh các module còn lại. Khi chuyển sang Sigmoid, agent tắt Filmic RGB hoặc Base Curve đang hoạt động để tránh áp hai lần tone mapping. Preview dùng cấu hình CLI tạm và sRGB, còn canvas hiển thị theo profile màn hình của Darktable.

Đối chiếu cấu trúc và mã đọc XMP tại [mã nguồn Darktable 5.6.2](https://github.com/darktable-org/darktable/tree/release-5.6.2/src).
