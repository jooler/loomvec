import { zodResolver } from '@hookform/resolvers/zod';
import { useForm } from 'react-hook-form';
import { useNavigate } from 'react-router';
import { z } from 'zod';
import { toast } from 'sonner';
import { useTranslation } from 'react-i18next';
import { t } from '@/i18n';
import { useAuth } from '@/auth';
import { Button } from '@loomvec/ui/components/ui/button';
import {
  Card,
  CardContent,
  CardDescription,
  CardHeader,
  CardTitle,
} from '@loomvec/ui/components/ui/card';
import { Input } from '@loomvec/ui/components/ui/input';
import { Label } from '@loomvec/ui/components/ui/label';

// 模块级文案（zod 校验消息）：main.tsx 已先初始化 i18n，import 阶段取值安全
const loginSchema = z.object({
  username: z.string().min(1, t('auth:usernameRequired')),
});

type LoginValues = z.infer<typeof loginSchema>;

/** dev 登录页；生产由 OIDC 接入。登录默认挂 operator（运营角色），
 *  平台角色单源为 DB user_role，无运营角色的账号在路由闸被拒。 */
export function LoginPage() {
  const { loginDev } = useAuth();
  const navigate = useNavigate();
  const { t } = useTranslation('auth');
  const form = useForm<LoginValues>({
    resolver: zodResolver(loginSchema),
    defaultValues: { username: 'ops' },
  });

  const onSubmit = form.handleSubmit(async (values) => {
    try {
      await loginDev(values.username, ['operator']);
      toast.success(t('loginSuccess'));
      navigate('/overview');
    } catch (e) {
      toast.error(e instanceof Error ? e.message : t('loginFailed'));
    }
  });

  return (
    <div className="grid min-h-svh place-items-center bg-muted/40 p-4">
      <Card className="w-full max-w-sm">
        <CardHeader>
          <CardTitle>{t('title')}</CardTitle>
          <CardDescription>{t('description')}</CardDescription>
        </CardHeader>
        <CardContent>
          <form onSubmit={onSubmit} className="space-y-4">
            <div className="space-y-2">
              <Label htmlFor="username">{t('usernameLabel')}</Label>
              <Input
                id="username"
                placeholder={t('usernamePlaceholder')}
                autoComplete="username"
                {...form.register('username')}
              />
              {form.formState.errors.username && (
                <p className="text-sm text-destructive">
                  {form.formState.errors.username.message}
                </p>
              )}
            </div>
            <Button type="submit" className="w-full" disabled={form.formState.isSubmitting}>
              {form.formState.isSubmitting ? t('loggingIn') : t('devLogin')}
            </Button>
          </form>
        </CardContent>
      </Card>
    </div>
  );
}
