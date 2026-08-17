"""Dịch mã lỗi của MiniMax sang câu người vận hành đọc được.

Vì sao cần: chuỗi này không nằm lại trong log. BE bọc nguyên văn vào response rồi FE hiện thẳng
trong toast — người bán hàng là người đọc nó. Bản cũ ném cả dict Python ra màn hình:

    No audio in Minimax response: {'base_resp': {'status_code': 2053, 'status_msg':
    'insufficient credit. Please purchase top-up credits or upgrade your subscription plan'}}

Quan trọng hơn: 2052 và 2053 KHÔNG phải lỗi kỹ thuật mà là giới hạn tài khoản, có người xử lý
được trong vài phút (nạp tiền, xoá bớt giọng). Đo ngày 13/08/2026, cả hai chức năng chính của
trang Clone giọng đều chết vì đúng hai mã này mà không chỗ nào cảnh báo — chỉ khi có người bấm
thử mới lộ ra.
"""

# Mã nào cũng phải nói được VIỆC CẦN LÀM. Nạp tiền không giải phóng được slot và ngược lại, nên
# hai mã 2052/2053 tuyệt đối không được gộp thành một câu chung chung.
MINIMAX_ERROR_MESSAGES = {
    2052: (
        'Tài khoản MiniMax đã hết slot chứa giọng clone. '
        'Vào dashboard MiniMax xoá bớt giọng cũ để giải phóng chỗ, hoặc nâng gói. '
        'Lưu ý: giọng đã xoá ở hệ thống này chưa chắc đã xoá bên MiniMax, vẫn có thể đang chiếm chỗ.'
    ),
    2053: (
        'Tài khoản MiniMax đã hết tiền. Cần nạp thêm credit hoặc nâng gói thì mới tạo được '
        'giọng nói. Thử lại khi chưa nạp thì vẫn ra đúng lỗi này.'
    ),
    2054: (
        'Giọng nói này không còn tồn tại trên MiniMax — có thể đã bị xoá ở đó nhưng vẫn còn '
        'trong danh sách của hệ thống. Chọn giọng khác hoặc clone lại.'
    ),
    1004: (
        'MiniMax từ chối xác thực (khoá API sai, hết hạn, hoặc không đúng group). '
        'Cần người quản trị kiểm tra MINIMAX_API_KEY, thử lại không giúp gì.'
    ),
}


def minimax_error_message(status_code, status_msg: str = '') -> str:
    """Câu báo cho một mã lỗi MiniMax.

    Mã đã biết thì trả câu tiếng Việt nói rõ việc cần làm. Mã lạ thì GIỮ NGUYÊN cả số lẫn nguyên
    văn tiếng Anh — thà câu dài còn hơn nuốt mất manh mối duy nhất để tra ra nó là lỗi gì.
    """
    known = MINIMAX_ERROR_MESSAGES.get(status_code)
    if known:
        # Giữ luôn mã số ở đuôi câu: người dùng cuối đọc phần tiếng Việt là đủ hiểu, còn người
        # vận hành vẫn tra được đúng mã trong tài liệu MiniMax và grep được trong log. Bỏ mã đi
        # là làm hỏng test_delete_cloned_voice — nó chốt đúng yêu cầu này.
        return f'{known} (mã MiniMax {status_code})'
    detail = f': {status_msg}' if status_msg else ''
    return f'MiniMax từ chối yêu cầu (mã {status_code}){detail}'


def minimax_error_from_response(data) -> str:
    """Đọc mã lỗi nằm trong `base_resp` của phản hồi MiniMax rồi dịch.

    Dùng cho ca MiniMax trả HTTP 200 nhưng không kèm audio — lỗi thật nằm trong thân phản hồi
    chứ không nằm ở mã HTTP.
    """
    base_resp = (data or {}).get('base_resp') if isinstance(data, dict) else None
    if not isinstance(base_resp, dict):
        return 'MiniMax không trả về dữ liệu âm thanh và cũng không nói lý do.'
    return minimax_error_message(base_resp.get('status_code', -1), base_resp.get('status_msg', ''))
