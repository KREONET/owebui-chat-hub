import yaml
import json
import base64
import subprocess
import sys
import os
import uuid

LITELLM_CONFIG_PATH = './litellm_config.yml'

def get_admin_user_id():
    cmd = [
        'docker', 'exec', 'owebui_db', 
        'psql', '-U', 'owebui', '-t', '-c', 
        "SELECT id FROM \"user\" WHERE role = 'admin' LIMIT 1;"
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print("Error fetching admin user ID:", result.stderr)
        sys.exit(1)
    admin_id = result.stdout.strip()
    if not admin_id:
        print("No admin user found.")
        sys.exit(1)
    return admin_id

def get_base64_image(filepath):
    if not os.path.exists(filepath):
        print(f"Warning: Image {filepath} not found.")
        return None
    with open(filepath, 'rb') as f:
        encoded = base64.b64encode(f.read()).decode('utf-8')
    ext = os.path.splitext(filepath)[1].lower()
    img_type = "png" if ext == ".png" else "jpeg" if ext in [".jpg", ".jpeg"] else "webp"
    return f"data:image/{img_type};base64,{encoded}"

def main():
    admin_id = get_admin_user_id()
    print(f"Admin User ID: {admin_id}")

    try:
        with open(LITELLM_CONFIG_PATH, 'r') as f:
            config = yaml.safe_load(f)
    except Exception as e:
        print(f"Failed to load config: {LITELLM_CONFIG_PATH}\nError: {e}")
        sys.exit(1)

    default_meta = config.get('owebui_default_model_meta', {})
    default_capabilities = default_meta.get('capabilities', {})
    default_builtin_tools = default_meta.get('builtinTools', {})
    
    models = config.get('model_list', [])
    statements = []
    
    for idx, m in enumerate(models, 1):
        model_id = m.get('model_name')
        if not model_id:
            continue
            
        owebui_params = m.get('owebui_params', {})
        name = owebui_params.get('name', model_id)
        vendor = owebui_params.get('vendor')
        logo_path = owebui_params.get('logo') or owebui_params.get('image')
        
        print(f"[{idx}/{len(models)}] 준비 중: {name} (ID: {model_id})")
        if logo_path:
            print(f"    - 로고 이미지 변환: {logo_path}")
        
        # Meta payload
        meta = {
            "description": owebui_params.get('description', ''),
            "capabilities": dict(default_capabilities)
        }
        if default_builtin_tools:
            meta["builtinTools"] = dict(default_builtin_tools)
        
        # 기존 capabilities 속성(하위호환성) 체크 및 신규 meta 항목 처리
        custom_caps = owebui_params.get('capabilities', {})
        meta["capabilities"].update(custom_caps)
        
        custom_meta = owebui_params.get('meta', {})
        if 'capabilities' in custom_meta:
            meta["capabilities"].update(custom_meta['capabilities'])
        if 'builtinTools' in custom_meta:
            meta["builtinTools"].update(custom_meta['builtinTools'])
        if 'defaultFeatureIds' in custom_meta:
            meta["defaultFeatureIds"] = custom_meta['defaultFeatureIds']
            
        # capabilities 에 있는 builtin_tools 가 False 라면 meta 의 builtinTools 딕셔너리를 제거
        if meta["capabilities"].get("builtin_tools") is False:
            meta.pop("builtinTools", None)
        
        # Override image_generation capability
        model_info = m.get('model_info', {})
        if model_info.get('mode') == 'image_generation':
            print("    - 이미지 생성 기능 활성화")
            meta["capabilities"]["image_generation"] = True
            
        if vendor:
            meta["tags"] = [{"name": vendor}]
            
        if logo_path:
            full_logo_path = os.path.join('/opt/chat', logo_path)
            b64_img = get_base64_image(full_logo_path)
            if b64_img:
                meta["profile_image_url"] = b64_img
                
        # Handle escaping for SQL
        meta_json = json.dumps(meta).replace("'", "''")
        name_safe = name.replace("'", "''")
        model_id_safe = model_id.replace("'", "''")
        
        sql = f"""
        INSERT INTO model (id, user_id, name, meta, params, created_at, updated_at)
        VALUES (
            '{model_id_safe}', 
            '{admin_id}', 
            '{name_safe}', 
            '{meta_json}',
            '{{}}',
            EXTRACT(EPOCH FROM NOW())::bigint,
            EXTRACT(EPOCH FROM NOW())::bigint
        )
        ON CONFLICT (id) DO UPDATE SET
            user_id = EXCLUDED.user_id,
            name = EXCLUDED.name,
            meta = EXCLUDED.meta,
            updated_at = EXCLUDED.updated_at;
            
        INSERT INTO access_grant (id, resource_type, resource_id, principal_type, principal_id, permission, created_at)
        SELECT '{str(uuid.uuid4())}', 'model', '{model_id_safe}', 'user', '*', 'read', EXTRACT(EPOCH FROM NOW())::bigint
        WHERE NOT EXISTS (
            SELECT 1 FROM access_grant 
            WHERE resource_type = 'model' 
              AND resource_id = '{model_id_safe}' 
              AND principal_type = 'user' 
              AND principal_id = '*' 
              AND permission = 'read'
        );
        """
        statements.append(sql)

    if not statements:
        print("No models found to sync.")
        return

    all_sql = "BEGIN;\n" + "\n".join(statements) + "\nCOMMIT;"
    
    # Execute all SQL
    cmd = [
        'docker', 'exec', '-i', 'owebui_db', 
        'psql', '-U', 'owebui', '-v', 'ON_ERROR_STOP=1'
    ]
    process = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    out, err = process.communicate(input=all_sql)
    
    if process.returncode != 0:
        print("Error executing SQL:\n", err)
    else:
        print(f"Successfully synced {len(statements)} models to the database.")

if __name__ == '__main__':
    main()
