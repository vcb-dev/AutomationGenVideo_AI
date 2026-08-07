<!--
TIÊU ĐỀ PR phải nói rõ TỪNG chức năng, không viết chung chung.

  Tốt:  feat(paast): tính điểm PAAST theo tỉ lệ primary/secondary của rule v1.1
  Tốt:  fix(scraper): timeout TikHub không nuốt lỗi, trả về mã lỗi thật
  Xấu:  update code / fix bug / sửa scraper

Một PR nhiều chức năng thì liệt kê từng cái ở mục "Chức năng trong PR này" bên dưới,
và mỗi chức năng phải có FILE UNIT TEST RIÊNG — không gộp nhiều chức năng vào một file.
-->

## Jira

- Ticket: <!-- VCBI-123 -->
- Link:

> Input và output của từng chức năng cập nhật trên Jira, không chép vào đây.
> Ghi ở đây link tới ticket đã cập nhật xong.

## Chức năng trong PR này

Mỗi chức năng một dòng, kèm đúng file test của riêng nó.

| # | Chức năng | File unit test |
|---|---|---|
| 1 |  | `tests/test_<ten_chuc_nang>.py` |
| 2 |  |  |

## Trước khi bấm "Ready for review"

- [ ] Tiêu đề PR nêu rõ từng chức năng, không viết chung chung
- [ ] Mỗi chức năng có **một file test riêng** trong `tests/` — không gộp
- [ ] Đã chạy file test tại máy và **đọc kết quả** (`python tests/test_<...>.py`)
- [ ] Input/output của từng chức năng đã cập nhật trên Jira

## Đã kiểm chứng thế nào

<!--
Dán output thật, đừng viết "đã test ok".
-->

```
```
