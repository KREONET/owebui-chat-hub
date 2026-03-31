"""
LiteLLM Custom Callback: Perplexity Citation Injector (본문 삽입 & 스트리밍 지원)

Perplexity 모델 응답의 search_results 를 추출하여
일반 응답 및 스트리밍 응답 모두 본문의 맨 끝에 출처 블록을 자동으로 추가합니다.
"""

import os
import json
import copy
import re
from urllib.parse import urlparse, unquote
from litellm.integrations.custom_logger import CustomLogger
from litellm.proxy.proxy_server import UserAPIKeyAuth
from typing import Optional, Any, AsyncGenerator

PERPLEXITY_MODEL_PREFIXES = ("bizrouter-perplexity", "perplexity")

# ──────────────────────────────────────────────
# Citation 헬퍼 함수들
# ──────────────────────────────────────────────

def _clean_text(text: str) -> str:
    """불필요한 투명 문자(0x200b 등) 및 중복 공백 제거"""
    if not text:
        return ""
    # \u200b(Zero Width Space) 및 기타 특수 제어 문자 제거
    text = re.sub(r'[\u200b\u200c\u200d\ufeff]', '', text)
    return text.strip()

def _get_domain_display(url: str) -> str:
    try:
        return urlparse(url).hostname or ""
    except Exception:
        return url

def _build_citation_block(search_results: list) -> str:
    if not search_results:
        return ""

    markdown = "\n\n---\n**🌐 참고 출처**\n"
    for i, sr in enumerate(search_results, 1):
        title   = _clean_text(sr.get("title") or f"출처 {i}")
        url     = sr.get("url", "")
        snippet = _clean_text(sr.get("snippet") or "")

        domain_disp = _get_domain_display(url)
        decoded_url = unquote(url) if url else ""

        # [1] 요구사항: 링크를 감싸는 볼드 제거
        markdown += f"\n[{i}] [{title}]({url})\n"

        # [2] 요구사항: 링크 바로 다음 줄에 인용 블록(>)이 오도록 줄바꿈 조정
        markdown += f"> **{domain_disp}**"
        if decoded_url:
            markdown += f" | `{decoded_url}`"
        
        if snippet:
            # snippet 내부의 줄바꿈은 공백으로 치환하여 한 줄로 표시
            clean_snippet = snippet.replace('\n', ' ')
            markdown += f"\n> {clean_snippet}\n"
        else:
            markdown += "\n"

    return markdown

# ──────────────────────────────────────────────
# Callback 핸들러
# ──────────────────────────────────────────────

class PerplexityCitationHandler(CustomLogger):

    # 1. 비스트리밍(Non-Streaming) 처리
    async def async_post_call_success_hook(
        self, data: dict, user_api_key_dict: UserAPIKeyAuth, response,
    ):
        model_name: str = data.get("model", "")
        if not any(model_name.startswith(p) for p in PERPLEXITY_MODEL_PREFIXES):
            return

        try:
            choices = getattr(response, "choices", [{}])
            message = getattr(choices[0], "message", None)
            if not message: return

            psf = getattr(message, "provider_specific_fields", {}) or {}
            metadata = psf.get("metadata") or getattr(message, "metadata", {}) or {}
            results = metadata.get("search_results", [])

            if results:
                message.content = (getattr(message, "content", "") or "") + _build_citation_block(results)
        except Exception:
            pass

    # 2. 스트리밍(Streaming) 처리
    async def async_post_call_streaming_iterator_hook(
        self, user_api_key_dict: UserAPIKeyAuth, response: Any, request_data: dict,
    ) -> AsyncGenerator:
        model_name = request_data.get("model", "")
        is_perplexity = any(model_name.startswith(p) for p in PERPLEXITY_MODEL_PREFIXES)

        last_chunk = None
        all_search_results = []

        async for chunk in response:
            last_chunk = chunk

            if is_perplexity:
                try:
                    if hasattr(chunk, "get"):
                        res = chunk.get("search_results")
                    else:
                        res = getattr(chunk, "search_results", None)

                    if res:
                        all_search_results = res
                    elif hasattr(chunk, "choices") and len(chunk.choices) > 0:
                        delta = getattr(chunk.choices[0], "delta", None)
                        if delta:
                            psf = getattr(delta, "provider_specific_fields", {}) or {}
                            md = psf.get("metadata") or getattr(delta, "metadata", {}) or {}
                            res = md.get("search_results", [])
                            if res:
                                all_search_results = res
                except Exception:
                    pass

            yield chunk

        if is_perplexity and all_search_results and last_chunk:
            citation_block = _build_citation_block(all_search_results)
            final_chunk = copy.deepcopy(last_chunk)
            if hasattr(final_chunk, "choices") and len(final_chunk.choices) > 0:
                delta = getattr(final_chunk.choices[0], "delta", None)
                if delta:
                    delta.content = citation_block
                    yield final_chunk

proxy_handler_instance = PerplexityCitationHandler()
