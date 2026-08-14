"""
Views for content generation based on viral videos.
"""
from rest_framework import status
from rest_framework.decorators import api_view
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from video_management.models import ScrapedVideo, GeneratedContent, Product
from video_management.services.content_generation_service import ContentGenerationService, DeepSeekError
import logging

# Câu báo cho từng loại lỗi DeepSeek. Viết tiếng Việt vì chuỗi này đi thẳng ra toast của người
# dùng cuối: BE lưu vào error_message rồi FE hiện nguyên văn.
#
# Mỗi câu phải nói được NÊN LÀM GÌ TIẾP. "Lỗi hệ thống" thì người dùng chỉ biết bấm lại, mà
# bấm lại với no_key hay client thì vô ích — sai chỗ nào phải nói đúng chỗ đó.
DEEPSEEK_ERROR_MESSAGES = {
    'no_key': 'Máy chủ AI chưa cấu hình DEEPSEEK_API_KEY. Cần người quản trị bổ sung, thử lại không giúp gì.',
    'client': 'DeepSeek từ chối yêu cầu — khoá sai, hết hạn hoặc hết số dư. Cần kiểm tra tài khoản DeepSeek.',
    'rate_limit': 'DeepSeek đang chặn vì quá nhiều lượt gọi cùng lúc. Chờ một lát rồi thử lại.',
    'timeout': 'DeepSeek không trả lời kịp. Thử lại, hoặc rút ngắn kịch bản nếu vẫn chậm.',
    'server': 'DeepSeek đang lỗi ở phía nhà cung cấp. Thử lại sau ít phút.',
    'network': 'Không kết nối được tới DeepSeek từ máy chủ AI.',
    'parse': 'DeepSeek trả về dữ liệu không đọc được. Thử lại một lượt nữa.',
    'empty': 'DeepSeek trả về nội dung rỗng. Thử lại hoặc diễn đạt kịch bản khác đi.',
    'unknown': 'Chuyển đổi thất bại vì lỗi chưa rõ ở phía DeepSeek. Xem log máy chủ AI để biết chi tiết.',
}

# Thiếu khoá là lỗi cấu hình của CHÍNH máy chủ này (5xx của mình), không phải lỗi nhà cung cấp
# bên ngoài — nên 500 chứ không 502. 429 giữ nguyên 429 để bên gọi biết mà giãn nhịp.
DEEPSEEK_ERROR_STATUS = {
    'no_key': status.HTTP_500_INTERNAL_SERVER_ERROR,
    'rate_limit': status.HTTP_429_TOO_MANY_REQUESTS,
}

logger = logging.getLogger(__name__)


@api_view(['POST'])
def generate_content(request):
    """
    Generate marketing content based on a viral video.
    
    POST /api/content/generate/
    Body:
    {
        "video_id": 123,  // optional if video_description provided
        "video_description": "...",  // optional if video_id provided
        "video_title": "...",  // optional
        "content_type": "A1",  // A1, A2, A3, A4, or A5
        "brand_name": "Viễn Chí Bảo",  // optional
        "industry": "kim hoàn",  // optional
        "additional_context": "..."  // optional
    }
    """
    try:
        # Get parameters
        video_id = request.data.get('video_id')
        video_description = request.data.get('video_description')
        video_title = request.data.get('video_title', '')
        content_type = request.data.get('content_type')
        brand_name = request.data.get('brand_name', 'Viễn Chí Bảo')
        industry = request.data.get('industry', 'kim hoàn (trang sức vàng bạc)')
        additional_context = request.data.get('additional_context')
        output_language = request.data.get('output_language', 'vi')  # default tiếng Việt
        target_market_language = request.data.get('target_market_language')
        tm_val = request.data.get('translation_mode', False)
        translation_mode = str(tm_val).lower() in ['true', '1', 't', 'yes'] if isinstance(tm_val, str) else bool(tm_val)
        custom_prompt = request.data.get('custom_prompt')
        if custom_prompt:
            additional_context = (additional_context or '') + ('\n\n' + custom_prompt if additional_context else custom_prompt)
        
        # NEW: Product information
        product_id = request.data.get('product_id')  # Optional product ID
        product_name = request.data.get('product_name')  # Optional product name
        product_category = request.data.get('product_category')  # Optional category
        product_description = request.data.get('product_description')  # Optional description
        product_price = request.data.get('product_price')  # Optional price
        product_sku = request.data.get('product_sku')  # Optional SKU (mã sản phẩm)

        # DEBUG: Log product info coming from FE so bạn thấy rõ trên terminal
        logger.info(
            f"🧾 Product from FE for content-generation: "
            f"id={product_id}, name='{product_name}', sku='{product_sku}', category='{product_category}', price='{product_price}'"
        )

        # Nếu FE chỉ gửi id (hoặc thiếu category/price/description) thì tự lấy đầy đủ từ catalog Product
        try:
            resolved_product = None
            if product_id:
                try:
                    resolved_product = Product.objects.filter(id=int(product_id)).first()
                except (ValueError, TypeError):
                    logger.warning(f"⚠️ Invalid product_id in content-generation: {product_id}")

            if not resolved_product and product_name:
                # Fallback: tìm theo tên nếu cần
                resolved_product = Product.objects.filter(name=product_name).order_by('-created_at').first()

            if resolved_product:
                if not product_name:
                    product_name = resolved_product.name
                if not product_category:
                    product_category = resolved_product.category
                if not product_description:
                    product_description = resolved_product.description
                if not product_price and resolved_product.price is not None:
                    product_price = str(resolved_product.price)
                if not product_sku and resolved_product.sku:
                    product_sku = resolved_product.sku

                logger.info(
                    f"✅ Product resolved for CONTENT pipeline: "
                    f"id={resolved_product.id}, name='{product_name}', sku='{product_sku}', "
                    f"category='{product_category}', price='{product_price}'"
                )
        except Exception as e:
            logger.error(f"Error resolving product from catalog for content-generation (id={product_id}): {e}", exc_info=True)
        
        # Validate
        if not content_type:
            return Response(
                {'error': 'content_type is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        if content_type not in ['A1', 'A2', 'A3', 'A4', 'A5']:
            return Response(
                {'error': 'content_type must be A1, A2, A3, A4, or A5'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Get video info from database OR from request
        source_video = None
        if video_id:
            try:
                source_video = ScrapedVideo.objects.get(id=video_id)
                video_description = source_video.description or source_video.title
                video_title = source_video.title
            except ScrapedVideo.DoesNotExist:
                logger.warning(f"Video ID {video_id} not found in database, using provided description")
        
        # If no video in DB, require description
        if not video_description:
            return Response(
                {'error': 'Either video_id (existing in DB) or video_description must be provided'},
                status=status.HTTP_400_BAD_REQUEST
            )
        
        # Build product_info dict for KOC prompt (tên, sku, category, mô tả → kể chuyện xoay quanh)
        product_info = None
        if product_name or product_sku or product_category or product_description:
            product_info = {
                'name': product_name or '',
                'sku': product_sku or '',
                'category': product_category or '',
                'description': product_description or '',
                'price': product_price or '',
            }
        
        # Combine additional context (user custom prompt etc.)
        combined_context = additional_context or ""
        
        # Generate content (KOC style - product_info được dùng trong prompt)
        service = ContentGenerationService()
        
        logger.info(f"Generating {content_type} content for: {video_title or 'Untitled'}")
        if product_name:
            logger.info(f"Product context: {product_name} (sku={product_sku}, {product_category})")
        
        result = service.generate_content(
            video_description=video_description,
            video_title=video_title or 'Video',
            content_type=content_type,
            brand_name=brand_name,
            industry=industry,
            additional_context=combined_context,
            product_info=product_info,
            output_language=output_language,
            target_market_language=target_market_language,
            translation_mode=translation_mode,
        )
        
        # Save to database (only if we have a source video)
        if source_video:
            generated_content = GeneratedContent.objects.create(
                source_video=source_video,
                content_type=content_type,
                title=result['title'],
                script=result['script'],
                hook=result['hook'],
                problem=result['problem'],
                solution=result['solution'],
                cta=result['cta'],
                word_count=result['word_count'],
                ai_model='gemini-2.5-flash',
                prompt_used=f"Brand: {brand_name}, Industry: {industry}, Type: {content_type}"
            )
            content_id = generated_content.id
            created_at = generated_content.created_at.isoformat()
        else:
            # No source video, return generated content without saving
            content_id = None
            from datetime import datetime
            created_at = datetime.now().isoformat()
        
        logger.info(f"Successfully generated content")
        
        # Return response
        return Response({
            'success': True,
            'content_id': content_id,
            'title': result['title'],
            'script': result['script'],
            'hook': result['hook'],
            'problem': result['problem'],
            'solution': result['solution'],
            'cta': result['cta'],
            'word_count': result['word_count'],
            'verification_rows': result.get('verification_rows', []),
            'source_insights': result.get('source_insights', {}),
            'content_type': content_type,
            'created_at': created_at
        }, status=status.HTTP_201_CREATED)
        
    except Exception as e:
        logger.error(f"Error generating content: {str(e)}", exc_info=True)
        return Response(
            {'error': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['POST'])
def generate_prompt(request):
    """
    Generate an optimized prompt based on video content.
    
    POST /api/content/generate-prompt/
    Body: same as generate_content
    """
    try:
        # Get parameters
        video_id = request.data.get('video_id')
        video_description = request.data.get('video_description')
        video_title = request.data.get('video_title', '')
        
        # Product info
        product_id = request.data.get('product_id')
        product_name = request.data.get('product_name')
        product_category = request.data.get('product_category')
        product_description = request.data.get('product_description')
        product_price = request.data.get('product_price')
        
        # Get video info if needed
        if video_id:
            try:
                source_video = ScrapedVideo.objects.get(id=video_id)
                video_description = source_video.description or source_video.title
                video_title = source_video.title
            except ScrapedVideo.DoesNotExist:
                pass
        
        if not video_description:
            return Response(
                {'error': 'Video description or ID is required'},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        product_info = None
        if product_name:
            product_info = {
                'name': product_name,
                'category': product_category,
                'description': product_description,
                'price': product_price
            }
            
        service = ContentGenerationService()
        prompt = service.generate_optimization_prompt(
            video_description=video_description,
            video_title=video_title,
            product_info=product_info
        )
        
        return Response({
            'success': True,
            'prompt': prompt
        })
        
    except Exception as e:
        logger.error(f"Error generating prompt: {str(e)}", exc_info=True)
        return Response(
            {'error': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['GET'])
def get_generated_contents(request, video_id):
    """
    Get all generated contents for a video.
    
    GET /api/content/video/<video_id>/
    """
    try:
        video = get_object_or_404(ScrapedVideo, id=video_id)
        contents = GeneratedContent.objects.filter(source_video=video).order_by('-created_at')
        
        data = [{
            'id': content.id,
            'content_type': content.content_type,
            'content_type_display': content.get_content_type_display(),
            'title': content.title,
            'script': content.script,
            'hook': content.hook,
            'problem': content.problem,
            'solution': content.solution,
            'cta': content.cta,
            'word_count': content.word_count,
            'is_approved': content.is_approved,
            'created_at': content.created_at.isoformat()
        } for content in contents]
        
        return Response({
            'success': True,
            'video_id': video_id,
            'contents': data
        })
        
    except Exception as e:
        logger.error(f"Error fetching generated contents: {str(e)}", exc_info=True)
        return Response(
            {'error': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['GET'])
def get_content_detail(request, content_id):
    """
    Get details of a generated content.
    
    GET /api/content/<content_id>/
    """
    try:
        content = get_object_or_404(GeneratedContent, id=content_id)
        
        return Response({
            'success': True,
            'id': content.id,
            'source_video_id': content.source_video.id,
            'source_video_title': content.source_video.title,
            'content_type': content.content_type,
            'content_type_display': content.get_content_type_display(),
            'title': content.title,
            'script': content.script,
            'hook': content.hook,
            'problem': content.problem,
            'solution': content.solution,
            'cta': content.cta,
            'word_count': content.word_count,
            'estimated_duration': content.estimated_duration,
            'is_approved': content.is_approved,
            'notes': content.notes,
            'created_at': content.created_at.isoformat()
        })
        
    except Exception as e:
        logger.error(f"Error fetching content detail: {str(e)}", exc_info=True)
        return Response(
            {'error': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )


@api_view(['POST'])
def transform_content(request):
    """
    Transform raw text based on a system prompt using DeepSeek.
    
    POST /api/ai/transform-content/
    Body:
    {
        "system_prompt": "...",
        "input_text": "..."
    }
    """
    try:
        system_prompt = request.data.get('system_prompt')
        input_text = request.data.get('input_text')
        
        if not system_prompt or not input_text:
            return Response(
                {'error': 'Both system_prompt and input_text are required'},
                status=status.HTTP_400_BAD_REQUEST
            )
            
        service = ContentGenerationService()

        # Dùng bản _checked để biết LOẠI lỗi. Bản _raw nuốt DeepSeekError rồi trả None, nên chỗ
        # này từng phải đoán bừa "Please check API key configuration" cho mọi trường hợp — kể cả
        # khi khoá vẫn đúng mà chỉ là hết số dư hay chạm rate limit. Câu đoán đó đi thẳng ra toast
        # cho người dùng cuối (BE lưu vào error_message, FE hiện nguyên văn), nên sai là họ đi
        # sửa nhầm chỗ.
        try:
            output_text = service._call_deepseek_checked(
                prompt=input_text,
                system_msg=system_prompt,
                log_prefix="Content transform (DeepSeek)"
            )
        except DeepSeekError as e:
            logger.warning(f"Content transform hỏng — kind={e.kind} status={e.status_code}: {e}")
            return Response(
                {'error': DEEPSEEK_ERROR_MESSAGES.get(e.kind, DEEPSEEK_ERROR_MESSAGES['unknown']),
                 'reason': e.kind,
                 'retriable': e.retriable},
                status=DEEPSEEK_ERROR_STATUS.get(e.kind, status.HTTP_502_BAD_GATEWAY)
            )

        if not output_text:
            return Response(
                {'error': DEEPSEEK_ERROR_MESSAGES['empty'], 'reason': 'empty', 'retriable': True},
                status=status.HTTP_502_BAD_GATEWAY
            )

        return Response({
            'success': True,
            'output_text': output_text
        })

    except Exception as e:
        logger.error(f"Error in transform_content view: {str(e)}", exc_info=True)
        return Response(
            {'error': str(e)},
            status=status.HTTP_500_INTERNAL_SERVER_ERROR
        )
