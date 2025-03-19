import base64
import json
from http import HTTPStatus
import typing as t

from django import http
from django.contrib.auth.models import User
from django.shortcuts import render
from django.utils import timezone as djtz
from django.views import View

import xmltodict

from app.src.exceptions import UserError
from app.src.layers.api.models import ApiModel, meddra, code_set
from app.src.layers.api.models.logging import Log
from app.src.layers.base.services import (
    BusinessServiceProtocol, 
    CIOMSServiceProtocol, 
    CodeSetServiceProtocol,
    MedDRAServiceProtocol
)
from extensions import utils


def log(method: t.Callable[[http.HttpRequest], http.HttpResponse]) \
-> t.Callable[[http.HttpRequest], http.HttpResponse]:
    
    def wrapper(self: View, request: http.HttpRequest, *args, **kwargs) -> http.HttpResponse:
        log = Log.create_from_request(request)

        exc = None
        try:
            response = method(self, request, *args, **kwargs)
            log.status = response.status_code
        except Exception as e:
            exc = e
            log.status = 500

        log.response_time = djtz.now()
        log.save()

        if exc:
            raise exc
        return response
    
    return wrapper


class AuthView(View):
    def dispatch(self, request: http.HttpRequest, *args, **kwargs) -> http.HttpResponse:
        print("DEBUG: Request headers:")
        for header, value in request.META.items():
            if header.startswith('HTTP_'):
                print(f"DEBUG:   {header}: {value}")
        try:
            auth_header = request.META['HTTP_AUTHORIZATION']
            print(f"DEBUG: Found authorization header: {auth_header}")
            encoded_credentials = auth_header.split(' ')[1]  # Remove "Basic " to isolate credentials
            decoded_credentials = base64.b64decode(encoded_credentials).decode('utf-8').split(':')
            username = decoded_credentials[0]
            password = decoded_credentials[1]
            print(f"DEBUG: Extracted username: {username}")
        except Exception as e:
            print(f"DEBUG: Authorization error: {str(e)}")
            return http.HttpResponse('Invalid HTTP_AUTHORIZATION header', status=HTTPStatus.UNAUTHORIZED)
        
        try:
            user = User.objects.get(username=username)
            is_valid = user.check_password(password)
            print(f"DEBUG: User found, password valid: {is_valid}")
        except User.DoesNotExist:
            print(f"DEBUG: User not found: {username}")
            is_valid = False

        if not is_valid:
            return http.HttpResponse('Invalid username or password', status=HTTPStatus.UNAUTHORIZED)
        
        request.user = user
        return super().dispatch(request, *args, **kwargs)


class AuthCheckView(AuthView):    
    def get(self, request: http.HttpRequest) -> http.HttpResponse:
        response = http.HttpResponse(
            json.dumps({"authenticated": True, "username": request.user.username}), 
            content_type="application/json"
        )
        response["Access-Control-Allow-Origin"] = "*"  # Или конкретный источник
        response["Access-Control-Allow-Credentials"] = "true"
        response["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        response["Access-Control-Allow-Headers"] = "Origin, Content-Type, Accept, Authorization"
        return response
    
    def options(self, request: http.HttpRequest) -> http.HttpResponse:
        # Обработка preflight запроса
        response = http.HttpResponse()
        response["Access-Control-Allow-Origin"] = "*"  # Или конкретный источник
        response["Access-Control-Allow-Credentials"] = "true"
        response["Access-Control-Allow-Methods"] = "GET, OPTIONS"
        response["Access-Control-Allow-Headers"] = "Origin, Content-Type, Accept, Authorization"
        return response

class BaseView(AuthView):
    domain_service: BusinessServiceProtocol[ApiModel] = ...
    model_class: type[ApiModel] = ...

    def dispatch(self, request: http.HttpRequest, *args, **kwargs) -> http.HttpResponse:
        try:
            return super().dispatch(request, *args, **kwargs)
        except (TypeError, json.JSONDecodeError):
            return http.HttpResponse('Invalid json data', status=HTTPStatus.BAD_REQUEST)
        except UserError as e:
            return http.HttpResponse(str(e), status=HTTPStatus.BAD_REQUEST)

    def get_model_from_request(self, request: http.HttpRequest) -> ApiModel:
        data = json.loads(request.body)
        model = self.model_class.model_dict_construct(data)
        return model.model_safe_validate(data)

    def get_status_code(self, is_ok: bool) -> HTTPStatus:
        return HTTPStatus.OK if is_ok else HTTPStatus.BAD_REQUEST

    def respond_with_model_as_json(self, model: ApiModel, status: HTTPStatus) -> http.HttpResponse:
        # Dump data and ignore warnings about wrong data format and etc.
        data = utils.exec_without_warnings(lambda: model.model_dump_json(by_alias=True))
        return self.respond_with_json(data, status)

    def respond_with_object_as_json(self, obj: t.Any, status: HTTPStatus) -> http.HttpResponse:
        return self.respond_with_json(json.dumps(obj), status)

    def respond_with_json(self, json_str: str, status: HTTPStatus) -> http.HttpResponse:
        return http.HttpResponse(json_str, status=status, content_type='application/json')


class ModelClassView(BaseView):
    def get(self, request: http.HttpRequest) -> http.HttpResponse:
        result_list = self.domain_service.list(self.model_class)
        return self.respond_with_object_as_json(result_list, HTTPStatus.OK)

    @log
    def post(self, request: http.HttpRequest) -> http.HttpResponse:
        model = self.get_model_from_request(request)
        if model.is_valid:
            # TODO: check id empty
            model, is_ok = self.domain_service.create(model)
        else:
            is_ok = False
        status = self.get_status_code(is_ok)
        return self.respond_with_model_as_json(model, status)


class ModelInstanceView(BaseView):
    def get(self, request: http.HttpRequest, pk: int) -> http.HttpResponse:
        model = self.domain_service.read(self.model_class, pk)
        return self.respond_with_model_as_json(model, HTTPStatus.OK)

    @log
    def put(self, request: http.HttpRequest, pk: int) -> http.HttpResponse:
        # TODO: check pk = model.id
        model = self.get_model_from_request(request)
        if model.is_valid:
            model, is_ok = self.domain_service.update(model, pk)
        else:
            is_ok = False
        status = self.get_status_code(is_ok)
        return self.respond_with_model_as_json(model, status)

    @log
    def delete(self, request: http.HttpRequest, pk: int) -> http.HttpResponse:
        is_ok = self.domain_service.delete(self.model_class, pk)
        status = self.get_status_code(is_ok)
        return http.HttpResponse(status=status)


class ModelBusinessValidationView(BaseView):
    def post(self, request: http.HttpRequest) -> http.HttpResponse:
        model = self.get_model_from_request(request)
        if model.is_valid:
            model, is_ok = self.domain_service.business_validate(model)
        else:
            is_ok = False
        status = self.get_status_code(is_ok)
        return self.respond_with_model_as_json(model, status)


class ModelToXmlView(BaseView):
    def post(self, request: http.HttpRequest) -> http.HttpResponse:
        model = self.get_model_from_request(request)
        model_dict = model.model_dump()
        self.extend_lists(model_dict)
        model_dict = {self.model_class.__name__: model_dict}
        result = xmltodict.unparse(model_dict)
        return http.HttpResponse(result, content_type='application/xml')

    # Is needed to fix issue with single item list in xmltodict lib
    @classmethod
    def extend_lists(cls, model_dict: dict[str, t.Any]) -> None:
        for value in model_dict.values():
            if isinstance(value, dict):
                cls.extend_lists(value)
            if isinstance(value, list) and len(value) == 1:
                value.append(dict())


class ModelFromXmlView(BaseView):
    def post(self, request: http.HttpRequest) -> http.HttpResponse:
        xml = json.loads(request.body)['value']
        model_dict = xmltodict.parse(xml)
        model_dict = model_dict[self.model_class.__name__]
        self.reduce_lists(model_dict)
        model = self.model_class(**model_dict)
        return self.respond_with_model_as_json(model, HTTPStatus.OK)

    # Is needed to fix issue with single item list in xmltodict lib
    @classmethod
    def reduce_lists(cls, model_dict: dict[str, t.Any]) -> None:
        for value in model_dict.values():
            if isinstance(value, dict):
                cls.reduce_lists(value)
            if isinstance(value, list) and len(value) == 2 and value[1] is None:
                value.pop(1)


class ModelCIOMSView(View):
    cioms_service: CIOMSServiceProtocol = ...

    def get(self, request: http.HttpRequest, pk: int) -> http.HttpResponse:
        return render(request, 'cioms.html', self.cioms_service.convert_icsr_to_cioms(pk))


class MedDRAReleaseView(View):
    meddra_service: MedDRAServiceProtocol = ...

    def get(self, request: http.HttpRequest) -> http.HttpResponse:
        objects = self.meddra_service.list()
        response = meddra.ReleaseResponse(
            root=[meddra.Release(id=obj.id, version=obj.version, language=obj.language) for obj in objects]
        )
        return http.HttpResponse(response.model_dump_json(), status=HTTPStatus.OK, content_type='application/json')


class MedDRASearchView(View):
    meddra_service: MedDRAServiceProtocol = ...

    def post(self, request: http.HttpRequest, pk: int) -> http.HttpResponse:
        search_request = meddra.SearchRequest.parse_raw(request.body)
        objects = self.meddra_service.search(search_request.search.level,
                                             search_request.state,
                                             search_request.search.input,
                                             pk)
        response = meddra.SearchResponse(terms=[meddra.Term(code=obj.code, name=obj.name) for obj in objects],
                                         level=search_request.search.level)
        return http.HttpResponse(response.model_dump_json(), status=HTTPStatus.OK, content_type='application/json')


class CodeSetView(View):
    code_set_service: CodeSetServiceProtocol = ...

    def get(self, request: http.HttpRequest, codeset: str) -> http.HttpResponse:
        objects = self.code_set_service.search(codeset,
                                               request.GET.get('q', ''),
                                               request.GET.get('lang', 'ENG'),
                                               request.GET.get('property', None))
        response = code_set.SearchResponse([code_set.Term(code=obj.code, name=obj.name) for obj in objects])
        return http.HttpResponse(response.model_dump_json(), status=HTTPStatus.OK, content_type='application/json')

    def post(self, request: http.HttpRequest, codeset: str) -> http.HttpResponse:
        file = request.FILES.get('file')
        if not file:
            return http.HttpResponse(status=HTTPStatus.BAD_REQUEST, content="File not uploaded")

        self.code_set_service.create(codeset, file, request.POST.get('lang', 'ENG'))
        return http.HttpResponse(status=HTTPStatus.CREATED)


class ExportMultipleXmlView(BaseView):
    def post(self, request: http.HttpRequest) -> http.HttpResponse:
        try:
            data = json.loads(request.body)
            icsr_ids = data.get('ids', [])
            
            if not icsr_ids:
                return http.HttpResponse('No ICSR IDs provided', status=HTTPStatus.BAD_REQUEST)
            
            icsr_list = []
            for icsr_id in icsr_ids:
                try:
                    icsr = self.domain_service.read(self.model_class, icsr_id)
                    icsr_list.append(icsr)
                except Exception as e:
                    print(f"Error retrieving ICSR {icsr_id}: {str(e)}")
                    continue
            
            if not icsr_list:
                return http.HttpResponse('None of the requested ICSRs could be retrieved', 
                                        status=HTTPStatus.NOT_FOUND)
            
            combined_xml = self.convert_multiple_to_xml(icsr_list)
            
            filename = f"e2b_export_{icsr_list[0].id}_{len(icsr_list)}_records_{djtz.now().strftime('%Y%m%d%H%M%S')}.xml"
            
            response = http.HttpResponse(combined_xml, content_type='application/xml')
            response['Content-Disposition'] = f'attachment; filename="{filename}"'
            
            return response
            
        except Exception as e:
            print(f"Error in ExportMultipleXmlView: {str(e)}")
            return http.HttpResponse(f'Error processing request: {str(e)}', 
                                    status=HTTPStatus.INTERNAL_SERVER_ERROR)
    
    def convert_multiple_to_xml(self, icsr_list: list) -> str:
        """
        Converts multiple ICSR models to a single E2B R3 XML file.
        
        Args:
            icsr_list: List of ICSR model instances to be converted
            
        Returns:
            XML string combining all ICSRs
        """
        xml_header = '<?xml version="1.0" encoding="UTF-8"?>\n'
        xml_header += '<ichicsr:ichicsr xmlns:ichicsr="urn:hl7-org:v3" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">\n'
        xml_header += '  <ichicsrmessageheader>\n'
        xml_header += f'    <messagetype>ichicsr</messagetype>\n'
        xml_header += f'    <messageformat>xml</messageformat>\n'
        xml_header += f'    <messageversion>2.1</messageversion>\n'
        xml_header += f'    <messageformatversion>2.1</messageformatversion>\n'
        xml_header += f'    <messagenumb>multi_{len(icsr_list)}_{djtz.now().strftime("%Y%m%d%H%M%S")}</messagenumb>\n'
        xml_header += f'    <messagesenderidentifier>E2B4Free</messagesenderidentifier>\n'
        xml_header += f'    <messagereceiveridentifier>Regulatory Authority</messagereceiveridentifier>\n'
        xml_header += f'    <messagedateformat>204</messagedateformat>\n'
        xml_header += f'    <messagedate>{djtz.now().strftime("%Y%m%d%H%M%S")}</messagedate>\n'
        xml_header += '  </ichicsrmessageheader>\n'
        
        safety_reports = []
        for icsr in icsr_list:
            model_dict = icsr.model_dump()
            
            self.extend_lists(model_dict)
            
            single_icsr_dict = {"safetyreport": model_dict}
            safety_report_xml = xmltodict.unparse(single_icsr_dict, pretty=True)
            
            safety_report_xml = safety_report_xml.replace('<?xml version="1.0" encoding="utf-8"?>', '')
            
            safety_reports.append(safety_report_xml)
        
        combined_body = "\n".join(safety_reports)
        
        xml_footer = '</ichicsr:ichicsr>'
        
        combined_xml = f"{xml_header}{combined_body}{xml_footer}"
        
        return combined_xml
    
    @classmethod
    def extend_lists(cls, model_dict: dict[str, t.Any]) -> None:
        for key, value in model_dict.items():
            if isinstance(value, dict):
                cls.extend_lists(value)
            if isinstance(value, list) and len(value) == 1:
                value.append(dict())

class ImportMultipleXmlView(BaseView):
    @classmethod
    def reduce_lists(cls, model_dict: dict[str, t.Any]) -> None:
        for value in model_dict.values():
            if isinstance(value, dict):
                cls.reduce_lists(value)
            if isinstance(value, list) and len(value) == 2 and value[1] is None:
                value.pop(1)

    @log
    def post(self, request: http.HttpRequest) -> http.HttpResponse:
        try:
            files_data = json.loads(request.body)
            xml_contents = files_data.get('files', [])
            
            if not xml_contents:
                return http.HttpResponse('No XML files provided', status=HTTPStatus.BAD_REQUEST)
            
            results = []
            
            for index, xml_content in enumerate(xml_contents):
                try:
                    model_dict = xmltodict.parse(xml_content)
                    
                    if 'ichicsr:ichicsr' in model_dict and 'safetyreport' in model_dict['ichicsr:ichicsr']:
                        safetyreport = model_dict['ichicsr:ichicsr']['safetyreport']
                    else:
                        raise ValueError("Could not find safetyreport in XML")
                    
                    processed_data = self.process_safetyreport(safetyreport)
                    
                    from app.src.layers.storage.models.icsr import ICSR, C_1_identification_case_safety_report
                    
                    try:
                        icsr = ICSR()
                        icsr.save()
                        
                        c1 = C_1_identification_case_safety_report(icsr=icsr)
                        
                        if 'c_1_identification_case_safety_report' in processed_data:
                            c1_data = processed_data['c_1_identification_case_safety_report']
                            
                            for field, value in c1_data.items():
                                if field != 'id' and value not in (None, ''):
                                    try:
                                        setattr(c1, field, value)
                                    except Exception as field_error:
                                        print(f"Error setting field {field}: {str(field_error)}")
                        
                        c1.save()
                        
                        self.create_related_objects(icsr, processed_data)
                        
                        results.append({
                            "success": True,
                            "id": icsr.id,
                            "filename": f"file_{index+1}.xml",
                            "message": "ICSR created successfully"
                        })
                        
                    except Exception as create_error:
                        print(f"Error creating ICSR: {str(create_error)}")
                        results.append({
                            "success": False,
                            "error": f"Creation error: {str(create_error)}",
                            "filename": f"file_{index+1}.xml"
                        })
                    
                except Exception as e:
                    print(f"Error processing XML file: {str(e)}")
                    results.append({
                        "success": False,
                        "error": f"Processing error: {str(e)}",
                        "filename": f"file_{index+1}.xml"
                    })
            
            response_data = {
                "results": results,
                "total": len(xml_contents),
                "successful": sum(1 for r in results if r.get("success", False)),
                "failed": sum(1 for r in results if not r.get("success", False))
            }
            
            return self.respond_with_object_as_json(response_data, HTTPStatus.OK)
            
        except Exception as e:
            print(f"Error in ImportMultipleXmlView: {str(e)}")
            return http.HttpResponse(f'Error processing request: {str(e)}', 
                                    status=HTTPStatus.INTERNAL_SERVER_ERROR)
        
    def process_safetyreport(self, safetyreport):
        """
        Process safetyreport XML structure to extract meaningful data.
        Handle the special structure with <value> and <null_flavor> tags.
        """
        processed_data = {}
        
        for key, value in safetyreport.items():
            if key == 'id' or key == 'errors':
                continue
                
            if isinstance(value, dict):
                processed_section = {}
                
                for field_key, field_value in value.items():
                    if field_key == 'id' or field_key == 'errors':
                        continue
                        
                    if isinstance(field_value, dict):
                        if 'value' in field_value:
                            if field_value['value'] not in (None, ''):
                                processed_section[field_key] = field_value['value']
                        elif 'null_flavor' in field_value and field_value['null_flavor'] not in (None, ''):
                            processed_section[f"nf_{field_key}"] = field_value['null_flavor']
                
                processed_data[key] = processed_section
                
        return processed_data
    
    def create_related_objects(self, icsr, processed_data):
        """
        Create related objects for the ICSR based on processed data.
        """
        from app.src.layers.storage.models.icsr import (
            C_3_information_sender_case_safety_report,
            C_5_study_identification,
            D_patient_characteristics,
            H_narrative_case_summary
        )
        
        if 'c_3_information_sender_case_safety_report' in processed_data:
            c3_data = processed_data['c_3_information_sender_case_safety_report']
            if c3_data:
                c3 = C_3_information_sender_case_safety_report(icsr=icsr)
                
                # Set fields
                for field, value in c3_data.items():
                    if field != 'id' and value not in (None, ''):
                        try:
                            setattr(c3, field, value)
                        except Exception as field_error:
                            print(f"Error setting {field} on C_3: {str(field_error)}")
                
                c3.save()
        
        if 'c_5_study_identification' in processed_data:
            c5_data = processed_data['c_5_study_identification']
            if c5_data:
                c5 = C_5_study_identification(icsr=icsr)
                
                for field, value in c5_data.items():
                    if field != 'id' and value not in (None, ''):
                        try:
                            setattr(c5, field, value)
                        except Exception as field_error:
                            print(f"Error setting {field} on C_5: {str(field_error)}")
                
                c5.save()
        
        if 'd_patient_characteristics' in processed_data:
            d_data = processed_data['d_patient_characteristics']
            if d_data:
                d = D_patient_characteristics(icsr=icsr)
                
                for field, value in d_data.items():
                    if field != 'id' and value not in (None, ''):
                        try:
                            setattr(d, field, value)
                        except Exception as field_error:
                            print(f"Error setting {field} on D: {str(field_error)}")
                
                d.save()
        
        if 'h_narrative_case_summary' in processed_data:
            h_data = processed_data['h_narrative_case_summary']
            if h_data:
                h = H_narrative_case_summary(icsr=icsr)
                
                for field, value in h_data.items():
                    if field != 'id' and value not in (None, ''):
                        try:
                            setattr(h, field, value)
                        except Exception as field_error:
                            print(f"Error setting {field} on H: {str(field_error)}")
                
                h.save()

