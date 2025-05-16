import { XML_API_CLIENT } from './clients';

export const XML_API = {
    validateExportXML: async (caseIds: Array<string | number>): Promise<{ [key: string]: { [field_id: string]: string } }[]> => {
        try {
            console.log('Validating export for cases:', caseIds);
            
            interface ValidationStatus {
                [fieldId: string]: string;
            }
    
            interface FileValidationStatus {
                success: boolean;
                "C.1.1": number | string;
                validation_status: ValidationStatus;
            }
    
            interface FileResponse {
                success: boolean;
                file_validation_status: FileValidationStatus[];
            }
    
            interface ApiResponse {
                results: FileResponse[];
                total: number;
                successful: number;
                failed: number;
            }
            
            console.log('Request payload:', { ids: caseIds, validation: true });
            
            const response = await XML_API_CLIENT.post<ApiResponse>('export-multiple', { 
                data: { ids: caseIds, validation: true }
            });
            
            console.log('Response from validation API:', response);
            
            if (!response || !response.results) {
                console.warn('Invalid response structure:', response);
                return [];
            }
    
            if (!response || !response.results) {
                return [];
            }
    
            const typedResponse = response.results.map((fileResponse: FileResponse) => {
                const fileValidationStatuses = fileResponse.file_validation_status || [];
                console.log('Check:', fileValidationStatuses);
                
                const formattedResult: { [key: string]: { [field_id: string]: string } } = {};
                
                fileValidationStatuses.forEach((status: FileValidationStatus) => {
                    const externalKey = String(status["C.1.1"] || "unknown");
                    
                    const validationStatus = status.validation_status || {};
                    console.log('Check2:', validationStatus);

                    formattedResult[externalKey] = {};
                    
                    Object.entries(validationStatus).forEach(([fieldId, errorMsg]) => {
                        formattedResult[externalKey][fieldId] = errorMsg;
                    });
                });
                

                return formattedResult;
            });
            
            return typedResponse;
        } catch (error) {
            console.error('Error validating export cases:', error);
            return [];
        }
    },

    validateImportXML: async (xmlContents: string[]): Promise<{ [key: string]: { [field_id: string]: string } }[]> => {
        try {
            console.log('Validating multiple import XML');
            
            interface ValidationStatus {
                [fieldId: string]: string;
            }
    
            interface FileValidationStatus {
                success: boolean;
                "C.1.1": number | string;
                validation_status: ValidationStatus;
            }
    
            interface FileResponse {
                success: boolean;
                file_validation_status: FileValidationStatus[];
            }
    
            interface ApiResponse {
                results: FileResponse[];
                total: number;
                successful: number;
                failed: number;
            }
            
            const response = await XML_API_CLIENT.post<ApiResponse>('import-multiple', { 
                data: { files: xmlContents, validation: true }
            });
    
            if (!response || !response.results) {
                return [];
            }
    
            const typedResponse = response.results.map((fileResponse: FileResponse) => {
                const fileValidationStatuses = fileResponse.file_validation_status || [];
                
                const formattedResult: { [key: string]: { [field_id: string]: string } } = {};
                
                fileValidationStatuses.forEach((status: FileValidationStatus) => {
                    const externalKey = String(status["C.1.1"] || "unknown");
                    
                    const validationStatus = status.validation_status || {};
                    
                    formattedResult[externalKey] = {};
                    
                    Object.entries(validationStatus).forEach(([fieldId, errorMsg]) => {
                        formattedResult[externalKey][fieldId] = errorMsg;
                    });
                });
                
                return formattedResult;
            });
            
            return typedResponse;
        } catch (error) {
            console.error('Error validating multiple XMLs:', error);
            return [];
        }
    },
    
    exportMultipleXml: async (ids: Array<string | number>): Promise<Blob> => {
        try {
            console.log("Exporting XML for multiple ICSRs:", ids);
            
            const response = await XML_API_CLIENT.post('export-multiple', { 
                data: { ids },
                responseFormat: 'blob'
            });
            
            if (response instanceof Blob) {
                return response;
            }
            
            const responseText = typeof response === 'string' 
                ? response 
                : JSON.stringify(response);
            
            return new Blob([responseText], { type: 'application/xml' });
        } catch (error) {
            console.error('Error exporting multiple XML:', error);
            throw error;
        }
    },
    
    importMultipleXml: async (xmlContents: Array<string>): Promise<any> => {
        try {
            console.log("Importing multiple XML files, count:", xmlContents.length);
            
            const response = await XML_API_CLIENT.post('import-multiple', { 
                data: { files: xmlContents }
            });
            
            return response;
        } catch (error) {
            console.error('Error importing multiple XML files:', error);
            throw error;
        }
    }
};
