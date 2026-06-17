# mcp_server/services/openai_service.py
"""
OpenAI API 서비스 (함수형)
"""
import os
import json
import re
import logging
import requests
from typing import Dict, Optional, List

logger = logging.getLogger(__name__)

# 전역 설정
_openai_config = None


def initialize_openai(config: dict) -> bool:
    """
    OpenAI 서비스 초기화
    
    Args:
        config: OpenAI 설정 딕셔너리
        
    Returns:
        bool: 초기화 성공 여부
    """
    global _openai_config
    
    _openai_config = config
    
    if not _openai_config.get('API_KEY'):
        logger.error("❌ OPENAI_API_KEY가 설정되지 않았습니다.")
        return False
    
    logger.info(f"✅ OpenAI 서비스 초기화 완료 - 모델: {_openai_config['MODEL']}")
    return True


def authenticate_openai(config: dict) -> bool:
    """
    OpenAI API 연결 테스트
    
    Args:
        config: OpenAI 설정 딕셔너리
        
    Returns:
        bool: 연결 성공 여부
    """
    global _openai_config
    
    if not _openai_config:
        initialize_openai(config)
    
    logger.info("OpenAI API 연결 테스트 중...")
    
    try:
        url = f"{_openai_config['BASE_URL']}/chat/completions"
        
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f"Bearer {_openai_config['API_KEY']}"
        }
        
        data = {
            "model": _openai_config['MODEL'],
            "messages": [
                {
                    "role": "user",
                    "content": "안녕하세요! 간단한 인사말로 답변해주세요."
                }
            ],
            "max_tokens": 50
        }
        
        response = requests.post(
            url,
            headers=headers,
            json=data,
            timeout=30
        )
        
        if response.status_code == 200:
            result = response.json()
            if 'choices' in result and len(result['choices']) > 0:
                logger.info("✅ OpenAI API 연결 테스트 성공")
                return True
        
        logger.error(f"❌ OpenAI API 연결 테스트 실패: {response.status_code}")
        return False
        
    except Exception as e:
        logger.error(f"❌ OpenAI API 테스트 실패: {e}", exc_info=True)
        return False


def generate_text(prompt: str, temperature: float = 0.7, max_tokens: int = 1024) -> Optional[str]:
    """
    텍스트 생성
    
    Args:
        prompt: 입력 프롬프트
        temperature: 생성 온도 (0.0-2.0)
        max_tokens: 최대 토큰 수
        
    Returns:
        Optional[str]: 생성된 텍스트
    """
    global _openai_config
    
    if not _openai_config:
        logger.error("❌ OpenAI 서비스가 초기화되지 않았습니다.")
        return None
    
    try:
        url = f"{_openai_config['BASE_URL']}/chat/completions"
        
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f"Bearer {_openai_config['API_KEY']}"
        }
        
        data = {
            "model": _openai_config['MODEL'],
            "messages": [
                {"role": "user", "content": prompt}
            ],
            "temperature": temperature,
            "max_tokens": max_tokens
        }
        
        response = requests.post(
            url,
            headers=headers,
            json=data,
            timeout=60
        )
        
        if response.status_code == 200:
            result = response.json()
            if 'choices' in result and len(result['choices']) > 0:
                text = result['choices'][0]['message']['content']
                logger.info("✅ 텍스트 생성 성공")
                return text
            else:
                logger.error("❌ 응답에서 텍스트를 찾을 수 없습니다.")
                return None
        else:
            logger.error(f"❌ 텍스트 생성 실패 ({response.status_code}): {response.text}")
            return None
            
    except Exception as e:
        logger.error(f"❌ 텍스트 생성 중 오류: {e}", exc_info=True)
        return None


def extract_customer_info(email_content: str, sender_email: str) -> Dict:
    """
    이메일에서 고객 정보 추출
    
    Args:
        email_content: 이메일 본문
        sender_email: 발신자 이메일
        
    Returns:
        Dict: 추출된 고객 정보
            - has_all_info: bool
            - name: str
            - company: str
            - title: str
            - phone: str
            - email: str
            - missing_fields: list
    """
    try:
        prompt = f"""
Analyze the following email content to extract customer information.
The content may include replies or forwarded messages. Ignore quoted text, previous email threads, and signatures. Focus only on the information provided in the most recent message part.

Email Content:
---
{email_content}
---

From the text above, extract the following fields and respond ONLY in a valid JSON format.
If a piece of information is not found, the value should be null.
The "email" field should default to the sender's email if not present in the body.

Sender's Email: {sender_email}

Required fields:
1. name: Full name of the person (e.g., "성춘향")
2. company: Company name (e.g., "춘향서비스")
3. title: Job title (e.g., "과장")
4. phone: Contact phone number (e.g., "010-2333-3333")
5. email: Contact email address

JSON response format:
{{
    "name": "value or null",
    "company": "value or null",
    "title": "value or null",
    "phone": "value or null",
    "email": "value or null"
}}
"""
        
        response_text = generate_text(prompt, temperature=0.3)
        
        if not response_text:
            raise Exception("OpenAI 응답 없음")
        
        # JSON 파싱
        content = response_text
        if '```json' in content:
            content = content.split('```json')[1].split('```')[0].strip()
        elif '```' in content:
            content = content.split('```')[1].split('```')[0].strip()
        
        # JSON에서 중괄호 부분만 추출
        json_match = re.search(r'\{[^}]+\}', content, re.DOTALL)
        if json_match:
            info = json.loads(json_match.group())
        else:
            info = json.loads(content)
        
        # 발신자 이메일 기본값 설정
        if not info.get('email') or info.get('email') == 'null':
            info['email'] = sender_email
        
        # 누락된 필드 확인
        required_fields = ['name', 'company', 'title', 'phone', 'email']
        missing_fields = []
        
        for field in required_fields:
            value = info.get(field)
            if not value or value == 'null' or value == '':
                missing_fields.append(field)
        
        result = {
            'has_all_info': len(missing_fields) == 0,
            'name': info.get('name') if info.get('name') != 'null' else None,
            'company': info.get('company') if info.get('company') != 'null' else None,
            'title': info.get('title') if info.get('title') != 'null' else None,
            'phone': info.get('phone') if info.get('phone') != 'null' else None,
            'email': info.get('email', sender_email),
            'missing_fields': missing_fields
        }
        
        logger.info(f"✅ 고객 정보 추출 완료: {result}")
        return result
        
    except Exception as e:
        logger.error(f"❌ 고객 정보 추출 실패: {e}", exc_info=True)
        return {
            'has_all_info': False,
            'name': None,
            'company': None,
            'title': None,
            'phone': None,
            'email': sender_email,
            'missing_fields': ['name', 'company', 'title', 'phone']
        }


def generate_reply(customer_info: Dict, original_subject: str) -> Dict:
    """
    고객 정보를 바탕으로 답변 생성
    
    Args:
        customer_info: 고객 정보 딕셔너리
        original_subject: 원본 이메일 제목
        
    Returns:
        Dict: 답변 이메일
            - subject: str
            - body: str
    """
    try:
        if customer_info['has_all_info']:
            # 모든 정보가 있는 경우
            prompt = f"""
고객이 다음 정보와 함께 문의했습니다:
- 이름: {customer_info['name']}
- 회사: {customer_info['company']}
- 직급: {customer_info['title']}
- 전화번호: {customer_info['phone']}
- 이메일: {customer_info['email']}

원본 제목: {original_subject}

다음 내용으로 정중한 답변 이메일을 작성해주세요:
1. 문의에 감사 인사
2. 고객님의 정보를 확인했다고 말하기
3. 담당 영업팀에 연결하여 신속히 연락드리겠다고 안내
4. 빠른 시일 내 연락드릴 것을 약속
5. "감사합니다" 마무리

전문적이고 친절한 톤으로 한국어로 작성하세요.
"""
        else:
            # 정보가 부족한 경우
            missing_kr = {
                'name': '성함',
                'company': '소속/회사명',
                'title': '직급',
                'phone': '연락처',
                'email': '이메일'
            }
            missing_list = [missing_kr.get(f, f) for f in customer_info['missing_fields']]
            
            prompt = f"""
고객이 문의 이메일을 보냈지만 다음 정보가 누락되었습니다:
{', '.join(missing_list)}

원본 제목: {original_subject}

다음 내용으로 정중한 답변 이메일을 작성해주세요:
1. 문의에 감사 인사
2. 정확한 상담을 위해 추가 정보가 필요하다고 설명
3. 누락된 정보 목록을 정중히 요청:
   - {chr(10).join(['   - ' + m for m in missing_list])}
4. 정보 제공 시 신속히 답변 드리겠다고 안내
5. "감사합니다" 마무리

전문적이고 친절한 톤으로 한국어로 작성하세요.
"""
        
        body = generate_text(prompt, temperature=0.7)
        
        if not body:
            raise Exception("답변 생성 실패")
        
        # 제목 생성
        if customer_info['has_all_info']:
            subject = f"Re: {original_subject} - 담당자 배정 완료"
        else:
            subject = f"Re: {original_subject} - 추가 정보 요청"
        
        logger.info(f"✅ 답변 생성 완료: {subject}")
        
        return {
            'subject': subject,
            'body': body
        }
        
    except Exception as e:
        logger.error(f"❌ 답변 생성 실패: {e}", exc_info=True)
        return {
            'subject': f"Re: {original_subject}",
            'body': "문의 주셔서 감사합니다. 빠른 시일 내에 답변 드리겠습니다."
        }


def generate_text_with_system(system_prompt: str, user_prompt: str, 
                              temperature: float = 0.7, max_tokens: int = 1024) -> Optional[str]:
    """
    시스템 프롬프트와 함께 텍스트 생성 (IT Helpdesk용)
    
    Args:
        system_prompt: 시스템 프롬프트
        user_prompt: 사용자 프롬프트
        temperature: 생성 온도
        max_tokens: 최대 토큰 수
        
    Returns:
        Optional[str]: 생성된 텍스트
    """
    global _openai_config
    
    if not _openai_config:
        logger.error("❌ OpenAI 서비스가 초기화되지 않았습니다.")
        return None
    
    try:
        url = f"{_openai_config['BASE_URL']}/chat/completions"
        
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f"Bearer {_openai_config['API_KEY']}"
        }
        
        data = {
            "model": _openai_config['MODEL'],
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "temperature": temperature,
            "max_tokens": max_tokens
        }
        
        response = requests.post(url, headers=headers, json=data, timeout=60)
        
        if response.status_code == 200:
            result = response.json()
            if 'choices' in result and len(result['choices']) > 0:
                return result['choices'][0]['message']['content']
        
        logger.error(f"❌ 텍스트 생성 실패: {response.status_code}")
        return None
        
    except Exception as e:
        logger.error(f"❌ 텍스트 생성 중 오류: {e}", exc_info=True)
        return None


def create_embedding(text: str) -> Optional[List[float]]:
    """
    텍스트의 임베딩 벡터 생성 (IT Helpdesk RAG용)
    
    Args:
        text: 임베딩할 텍스트
        
    Returns:
        Optional[List[float]]: 임베딩 벡터
    """
    global _openai_config
    
    if not _openai_config:
        logger.error("❌ OpenAI 서비스가 초기화되지 않았습니다.")
        return None
    
    try:
        url = f"{_openai_config['BASE_URL']}/embeddings"
        
        headers = {
            'Content-Type': 'application/json',
            'Authorization': f"Bearer {_openai_config['API_KEY']}"
        }
        
        data = {
            "model": "text-embedding-3-small",
            "input": text
        }
        
        response = requests.post(url, headers=headers, json=data, timeout=30)
        
        if response.status_code == 200:
            result = response.json()
            if 'data' in result and len(result['data']) > 0:
                return result['data'][0]['embedding']
        
        logger.error(f"❌ 임베딩 생성 실패: {response.status_code}")
        return None
        
    except Exception as e:
        logger.error(f"❌ 임베딩 생성 중 오류: {e}", exc_info=True)
        return None


def get_service_status() -> Dict:
    """
    OpenAI 서비스 상태 확인
    
    Returns:
        Dict: 상태 정보
    """
    global _openai_config
    
    return {
        'initialized': _openai_config is not None,
        'api_key_configured': _openai_config.get('API_KEY') is not None if _openai_config else False,
        'model': _openai_config.get('MODEL') if _openai_config else None
    }